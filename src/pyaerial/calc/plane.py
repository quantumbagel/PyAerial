"""
Per-plane calculations and live geofence alerting.

Runs each tick on every tracked plane: derives speed/heading from position
history, optionally enriches callsign metadata, and manages active/inactive
alert state with lifecycle alerter hooks.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import math
import threading
import time
from typing import TYPE_CHECKING

import requests
from shapely import Polygon

from pyaerial.alerters import Alerter, create_alerter
from pyaerial.calc import evaluate, geo
from pyaerial.calc.aircraft_db import AircraftDB
from pyaerial.calc.kalman import KinematicKalmanFilter
from pyaerial.config.schema import AlertActionConfig, Config, RuleConfig
from pyaerial.store.redis_live import RedisLiveStore
from pyaerial.constants import (
    ALERT_CAT_ETA,
    ALERT_CAT_REASON,
    ALERT_CAT_TYPE,
    ALERT_CAT_ZONE,
    STORE_ALT,
    STORE_CALC_DATA,
    STORE_CALLSIGN,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_INFO,
    STORE_LAT,
    STORE_LONG,
    STORE_RECV_DATA,
)
from pyaerial.models import Datum, get_latest, patch_append
from pyaerial.store.mongo import flight_id_for_plane

if TYPE_CHECKING:
    pass

log = logging.getLogger("pyaerial.calc.plane")

_ETA_HORIZON = 10_000
_ALERT_HOOK_ACTIVATE = "activate"
_ALERT_HOOK_DEACTIVATE = "deactivate"
_ALERT_HOOK_WHILE_ACTIVE = "while_active"


class PlaneCalculator:
    """Stateful calculator with cached alerters and optional aircraft metadata."""

    def __init__(self, config: Config, polygons: dict[str, Polygon],
                 aircraft_db: AircraftDB | None = None,
                 store: RedisLiveStore | None = None):
        self.config = config
        self.polygons = polygons
        self.aircraft_db = aircraft_db
        self.store = store
        self.backdate = config.tracking.backdate_packets
        self._alerters: dict[tuple[str, str], Alerter] = {}
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="callsign-lookup")
        self._alert_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="alert-dispatch")
        self._pending_lookups: set[str] = set()
        self._lock = threading.Lock()
        self._kalman_filters: dict[str, KinematicKalmanFilter] = {}
        # (icao, zone, rule) -> {activated_at, last_periodic, alert_id}
        self._alert_state: dict[tuple[str, str, str], dict] = {}

    def close(self) -> None:
        for alerter in self._alerters.values():
            alerter.close()
        self._alerters.clear()
        self._executor.shutdown(wait=False)
        self._alert_executor.shutdown(wait=False)

    def calculate_all(self, planes: dict[str, dict], dirty_icaos: set[str] | None = None) -> None:
        if dirty_icaos is not None:
            # Calculate for dirty planes + active alert planes (for periodic checks)
            active_alert_icaos = {key[0] for key in self._alert_state}
            target_icaos = dirty_icaos | active_alert_icaos
            for icao, plane in planes.items():
                if icao.lower() in target_icaos or icao.upper() in target_icaos:
                    self.calculate_plane(plane)
        else:
            for plane in planes.values():
                self.calculate_plane(plane)

    def calculate_plane(self, plane: dict) -> None:
        recv = plane.get(STORE_RECV_DATA, {})
        if STORE_LAT not in recv or STORE_LONG not in recv:
            return

        lat_series = recv[STORE_LAT]
        lon_series = recv[STORE_LONG]
        if len(lat_series) < 2:
            return

        if len(lat_series) < self.backdate:
            previous_lat = lat_series[0]
            previous_lon = lon_series[0]
        else:
            previous_lat = lat_series[-self.backdate]
            previous_lon = get_latest(STORE_RECV_DATA, STORE_LONG, plane,
                                      previous_lat.time) or lon_series[0]

        previous = (previous_lat.value, previous_lon.value)
        previous_time = previous_lat.time
        current_lat = lat_series[-1]
        current_lon = lon_series[-1]
        current = (current_lat.value, current_lon.value)
        current_time = current_lat.time

        speed = geo.calculate_speed(previous, current, previous_time, current_time)
        heading = geo.calculate_heading(previous, current)

        final_speed, speed_time = self._choose_speed(plane, speed, current_time)
        final_heading = self._choose_heading(plane, heading, current_time)

        prev_speed_series = plane.get(STORE_CALC_DATA, {}).get(STORE_HORIZ_SPEED, [])
        if prev_speed_series:
            alpha = 0.3
            final_speed = alpha * final_speed + (1.0 - alpha) * prev_speed_series[-1].value

        prev_heading_series = plane.get(STORE_CALC_DATA, {}).get(STORE_HEADING, [])
        if prev_heading_series:
            alpha = 0.3
            prev_heading = prev_heading_series[-1].value
            rad_current = math.radians(final_heading)
            rad_prev = math.radians(prev_heading)
            sin_val = alpha * math.sin(rad_current) + (1.0 - alpha) * math.sin(rad_prev)
            cos_val = alpha * math.cos(rad_current) + (1.0 - alpha) * math.cos(rad_prev)
            final_heading = (math.degrees(math.atan2(sin_val, cos_val)) + 360.0) % 360.0

        # Run Kalman filter update
        icao = plane[STORE_INFO][STORE_ICAO].lower()
        kf = self._kalman_filters.get(icao)
        dt = current_time - previous_time if previous_time > 0 else 0.0
        if kf is None:
            kf = KinematicKalmanFilter(current[0], current[1])
            self._kalman_filters[icao] = kf
        else:
            kf.update(current[0], current[1], dt)

        patch_append(plane, STORE_CALC_DATA, STORE_HORIZ_SPEED,
                     Datum(final_speed, speed_time))
        patch_append(plane, STORE_CALC_DATA, STORE_HEADING,
                     Datum(final_heading, speed_time))

        callsign = self._resolve_callsign(plane)
        self._check_alerts(plane, current, final_heading, final_speed, callsign)

    def deactivate_plane(self, plane: dict) -> None:
        """Deactivate and clean up all active alerts for a plane being removed or expired."""
        info = plane.get(STORE_INFO, {})
        if STORE_ICAO not in info:
            return
        icao = info[STORE_ICAO].lower()
        stale_keys = [k for k in self._alert_state if k[0] == icao]
        if not stale_keys:
            return

        now = time.time()
        callsign = info.get(STORE_CALLSIGN) or ""
        recv = plane.get(STORE_RECV_DATA, {})
        lat_series = recv.get(STORE_LAT, [])
        lon_series = recv.get(STORE_LONG, [])
        if lat_series and lon_series:
            position = (lat_series[-1].value, lon_series[-1].value)
        else:
            position = (0.0, 0.0)

        geofence_etas = {z: math.inf for z in self.config.zones}
        for key in stale_keys:
            zone_name, rule_name = key[1], key[2]
            state = self._alert_state.pop(key)
            zone = self.config.zones.get(zone_name)
            rule = next((r for r in zone.rules if r.name == rule_name), None) if zone else None
            if rule is not None:
                self._on_deactivate(
                    plane, zone_name, rule, math.inf, geofence_etas,
                    position, callsign, now, state["alert_id"], state["activated_at"],
                )
            elif self.store is not None:
                self.store.record_alert_episode(
                    plane,
                    {
                        STORE_ICAO: icao,
                        STORE_CALLSIGN: callsign,
                        ALERT_CAT_TYPE: rule_name,
                        ALERT_CAT_ZONE: zone_name,
                        ALERT_CAT_ETA: math.inf,
                    },
                    self._build_payload(plane, position),
                    alert_id=state["alert_id"],
                    activated_at=state["activated_at"],
                    deactivated_at=now,
                    active=False,
                )
        plane["active_alerts"] = []

    def _choose_speed(self, plane: dict, computed: float, current_time: float) -> tuple[float, float]:
        recv = plane.get(STORE_RECV_DATA, {})
        if STORE_HORIZ_SPEED not in recv:
            return computed, current_time
        reported = recv[STORE_HORIZ_SPEED][-1]
        if current_time - reported.time < self.backdate:
            return reported.value, reported.time
        return computed, current_time

    def _choose_heading(self, plane: dict, computed: float, current_time: float) -> float:
        recv = plane.get(STORE_RECV_DATA, {})
        if STORE_HEADING not in recv:
            return computed
        reported = recv[STORE_HEADING][-1]
        if current_time - reported.time < self.backdate:
            return reported.value
        return computed

    def _resolve_callsign(self, plane: dict) -> str:
        info = plane[STORE_INFO]
        if info.get("metadata_resolved"):
            return info.get(STORE_CALLSIGN) or ""

        icao = info[STORE_ICAO]
        with self._lock:
            if icao in self._pending_lookups:
                return info.get(STORE_CALLSIGN) or ""
            self._pending_lookups.add(icao)

        self._executor.submit(self._bg_lookup_metadata, plane, icao)
        return info.get(STORE_CALLSIGN) or ""

    def _bg_lookup_metadata(self, plane: dict, icao: str) -> None:
        try:
            callsign = plane[STORE_INFO].get(STORE_CALLSIGN)
            model = ""
            owner = ""
            country = ""
            aircraft_type = ""

            if self.aircraft_db and self.aircraft_db.available:
                record = self.aircraft_db.lookup_cached(icao)
                if record:
                    if not callsign:
                        callsign = record.get("callsign") or record.get("registration")
                    model = record.get("model") or ""
                    owner = record.get("owner") or ""
                    country = record.get("country") or ""
                    aircraft_type = record.get("typecode") or ""

            plane[STORE_INFO][STORE_CALLSIGN] = callsign or ""
            plane[STORE_INFO]["model"] = model
            plane[STORE_INFO]["owner"] = owner
            plane[STORE_INFO]["country"] = country
            plane[STORE_INFO]["aircraft_type"] = aircraft_type
            plane[STORE_INFO]["typecode"] = aircraft_type
            plane[STORE_INFO]["metadata_resolved"] = True
        except Exception as exc:
            log.debug("Background metadata lookup failed for %s: %s", icao, exc)
            plane[STORE_INFO].setdefault(STORE_CALLSIGN, "")
            plane[STORE_INFO].setdefault("model", "")
            plane[STORE_INFO].setdefault("owner", "")
            plane[STORE_INFO].setdefault("country", "")
            plane[STORE_INFO].setdefault("aircraft_type", "")
            plane[STORE_INFO].setdefault("typecode", "")
            plane[STORE_INFO]["metadata_resolved"] = True
        finally:
            with self._lock:
                self._pending_lookups.discard(icao)

    def _check_alerts(self, plane: dict, position: tuple[float, float],
                      heading: float, speed: float, callsign: str) -> None:
        icao = plane[STORE_INFO][STORE_ICAO].lower()
        flight_id = flight_id_for_plane(plane)
        now = time.time()
        geofence_etas: dict[str, float] = {}
        matching: dict[tuple[str, str, str], tuple[str, RuleConfig, float]] = {}

        for zone_name, zone in self.config.zones.items():
            polygon = self.polygons[zone_name]
            eta = geo.time_to_enter_geofence(position, heading, speed, polygon, _ETA_HORIZON)
            geofence_etas[zone_name] = eta

            resolver = evaluate.make_resolver(plane, eta, polygon, position)
            for rule in zone.rules:
                if not evaluate.when_passes(rule.when, resolver):
                    continue
                key = (icao, zone_name, rule.name)
                matching[key] = (zone_name, rule, eta)

        active_alerts: list[dict] = []
        for key, (zone_name, rule, eta) in matching.items():
            state = self._alert_state.get(key)
            if state is None:
                alert_id = f"{flight_id}:{zone_name}:{rule.name}"
                state = {
                    "activated_at": now,
                    "last_periodic": 0.0,
                    "alert_id": alert_id,
                }
                self._alert_state[key] = state
                self._on_activate(plane, zone_name, rule, eta, geofence_etas,
                                position, callsign, now, alert_id)
            else:
                self._refresh_active_alert(
                    plane, zone_name, rule, eta, geofence_etas,
                    position, callsign, now, state["alert_id"],
                )
                if rule.while_active is not None:
                    interval = rule.while_active.interval_seconds
                    if now - state["last_periodic"] >= interval:
                        state["last_periodic"] = now
                        self._on_while_active(plane, zone_name, rule, eta, geofence_etas,
                                              position, callsign, now, state["alert_id"])

            active_alerts.append({
                "alert_id": state["alert_id"],
                "zone": zone_name,
                "rule": rule.name,
                "activated_at": state["activated_at"],
                "eta": eta,
            })

        stale_keys = [key for key in self._alert_state if key[0] == icao and key not in matching]
        for key in stale_keys:
            zone_name, rule_name = key[1], key[2]
            state = self._alert_state.pop(key)
            zone = self.config.zones.get(zone_name)
            rule = next((r for r in zone.rules if r.name == rule_name), None) if zone else None
            eta = geofence_etas.get(zone_name, math.inf)
            if rule is not None:
                self._on_deactivate(plane, zone_name, rule, eta, geofence_etas,
                                    position, callsign, now, state["alert_id"],
                                    state["activated_at"])
            elif self.store is not None:
                self.store.record_alert_episode(
                    plane,
                    {
                        STORE_ICAO: icao,
                        STORE_CALLSIGN: callsign,
                        ALERT_CAT_TYPE: rule_name,
                        ALERT_CAT_ZONE: zone_name,
                        ALERT_CAT_ETA: eta,
                    },
                    self._build_payload(plane, position),
                    alert_id=state["alert_id"],
                    activated_at=state["activated_at"],
                    deactivated_at=now,
                    active=False,
                )

        plane["active_alerts"] = active_alerts

    def _build_meta(self, plane: dict, zone_name: str, rule: RuleConfig, eta: float,
                    geofence_etas: dict[str, float], callsign: str,
                    hook: str) -> dict:
        return {
            STORE_ICAO: plane[STORE_INFO][STORE_ICAO],
            STORE_CALLSIGN: callsign,
            ALERT_CAT_TYPE: rule.name,
            ALERT_CAT_ZONE: zone_name,
            ALERT_CAT_ETA: eta,
            ALERT_CAT_REASON: {
                "zones": geofence_etas,
                "rule": rule.name,
                "hook": hook,
            },
        }

    def _build_payload(self, plane: dict, position: tuple[float, float]) -> dict:
        alt = get_latest(STORE_RECV_DATA, STORE_ALT, plane)
        return {
            STORE_LAT: position[0],
            STORE_LONG: position[1],
            STORE_ALT: alt.value if alt else None,
        }

    def _run_actions(self, actions: list[AlertActionConfig], meta: dict, payload: dict) -> None:
        for action in actions:
            alerter = self._get_alerter(action.method, action.options)
            self._alert_executor.submit(alerter.alert, meta, payload)

    def _on_activate(self, plane: dict, zone_name: str, rule: RuleConfig, eta: float,
                     geofence_etas: dict[str, float], position: tuple[float, float],
                     callsign: str, now: float, alert_id: str) -> None:
        meta = self._build_meta(plane, zone_name, rule, eta, geofence_etas, callsign,
                                _ALERT_HOOK_ACTIVATE)
        payload = self._build_payload(plane, position)
        if rule.on_activate:
            self._run_actions(rule.on_activate, meta, payload)
        if self.store is not None:
            self.store.record_alert_episode(
                plane, meta, payload, alert_id=alert_id,
                activated_at=now, active=True,
            )

    def _on_deactivate(self, plane: dict, zone_name: str, rule: RuleConfig, eta: float,
                       geofence_etas: dict[str, float], position: tuple[float, float],
                       callsign: str, now: float, alert_id: str,
                       activated_at: float) -> None:
        meta = self._build_meta(plane, zone_name, rule, eta, geofence_etas, callsign,
                                _ALERT_HOOK_DEACTIVATE)
        payload = self._build_payload(plane, position)
        if rule.on_deactivate:
            self._run_actions(rule.on_deactivate, meta, payload)
        if self.store is not None:
            self.store.record_alert_episode(
                plane, meta, payload, alert_id=alert_id,
                activated_at=activated_at, deactivated_at=now, active=False,
            )

    def _refresh_active_alert(self, plane: dict, zone_name: str, rule: RuleConfig, eta: float,
                              geofence_etas: dict[str, float], position: tuple[float, float],
                              callsign: str, now: float, alert_id: str) -> None:
        if self.store is None:
            return
        meta = self._build_meta(plane, zone_name, rule, eta, geofence_etas, callsign,
                                _ALERT_HOOK_WHILE_ACTIVE)
        payload = self._build_payload(plane, position)
        self.store.update_active_alert(plane, alert_id, meta, payload, now)

    def _on_while_active(self, plane: dict, zone_name: str, rule: RuleConfig, eta: float,
                         geofence_etas: dict[str, float], position: tuple[float, float],
                         callsign: str, now: float, alert_id: str) -> None:
        while_active = rule.while_active
        if while_active is None or not while_active.actions:
            return
        meta = self._build_meta(plane, zone_name, rule, eta, geofence_etas, callsign,
                                _ALERT_HOOK_WHILE_ACTIVE)
        payload = self._build_payload(plane, position)
        self._run_actions(while_active.actions, meta, payload)

    def _get_alerter(self, method: str, arguments: dict) -> Alerter:
        key = (method, str(sorted(arguments.items())))
        if key not in self._alerters:
            self._alerters[key] = create_alerter(method, arguments)
        return self._alerters[key]
