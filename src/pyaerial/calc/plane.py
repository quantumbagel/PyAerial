"""
Per-plane calculations and live geofence alerting.

Runs each tick on every tracked plane: derives speed/heading from position
history, optionally enriches callsign metadata, and fires alerters when zone
level requirements are satisfied.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import math
import threading
from typing import TYPE_CHECKING

import requests
from shapely import Polygon

from pyaerial.alerters import Alerter, create_alerter
from pyaerial.calc import evaluate, geo
from pyaerial.calc.aircraft_db import AircraftDB
from pyaerial.config.schema import Config
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

if TYPE_CHECKING:
    pass

log = logging.getLogger("pyaerial.calc.plane")

_ETA_HORIZON = 10_000


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
        # Concurrency for non-blocking API lookups of aircraft callsigns/registrations
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="callsign-lookup")
        self._pending_lookups: set[str] = set()
        self._lock = threading.Lock()

    def close(self) -> None:
        for alerter in self._alerters.values():
            alerter.close()
        self._alerters.clear()
        self._executor.shutdown(wait=False)

    def calculate_all(self, planes: dict[str, dict]) -> None:
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

        # Smooth speed and heading using Exponential Moving Average (EMA) to filter out jitter/noise
        prev_speed_series = plane.get(STORE_CALC_DATA, {}).get(STORE_HORIZ_SPEED, [])
        if prev_speed_series:
            alpha = 0.3  # Higher values = faster response, lower values = more smoothing
            final_speed = alpha * final_speed + (1.0 - alpha) * prev_speed_series[-1].value

        prev_heading_series = plane.get(STORE_CALC_DATA, {}).get(STORE_HEADING, [])
        if prev_heading_series:
            alpha = 0.3
            prev_heading = prev_heading_series[-1].value
            # Correctly handle angular wrap-around at 360/0 degrees by averaging unit vectors
            rad_current = math.radians(final_heading)
            rad_prev = math.radians(prev_heading)
            sin_val = alpha * math.sin(rad_current) + (1.0 - alpha) * math.sin(rad_prev)
            cos_val = alpha * math.cos(rad_current) + (1.0 - alpha) * math.cos(rad_prev)
            final_heading = (math.degrees(math.atan2(sin_val, cos_val)) + 360.0) % 360.0

        patch_append(plane, STORE_CALC_DATA, STORE_HORIZ_SPEED,
                     Datum(final_speed, speed_time))
        patch_append(plane, STORE_CALC_DATA, STORE_HEADING,
                     Datum(final_heading, speed_time))

        callsign = self._resolve_callsign(plane)
        self._check_alerts(plane, current, final_heading, final_speed, callsign)

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
            if not callsign:
                callsign = _lookup_callsign_hexdb(icao)

            model = ""
            owner = ""
            country = ""
            aircraft_type = ""

            if self.aircraft_db and self.aircraft_db.available:
                record = self.aircraft_db.lookup(icao)
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
        plane["zone"] = ""
        plane["level"] = ""
        geofence_etas: dict[str, float] = {}

        for zone_name, zone in self.config.zones.items():
            polygon = self.polygons[zone_name]
            eta = geo.time_to_enter_geofence(position, heading, speed, polygon, _ETA_HORIZON)
            geofence_etas[zone_name] = eta

            resolver = evaluate.make_resolver(plane, eta, polygon, position)
            for rule in zone.rules:
                if not evaluate.when_passes(rule.when, resolver):
                    continue

                if plane.get("level") != "alert":
                    plane["zone"] = zone_name
                    plane["level"] = rule.name

                alerter = self._get_alerter(rule.alert.method, rule.alert.options)
                alt = get_latest(STORE_RECV_DATA, STORE_ALT, plane)
                payload = {
                    STORE_LAT: position[0],
                    STORE_LONG: position[1],
                    STORE_ALT: alt.value if alt else None,
                }
                meta = {
                    STORE_ICAO: plane[STORE_INFO][STORE_ICAO],
                    STORE_CALLSIGN: callsign,
                    ALERT_CAT_TYPE: rule.name,
                    ALERT_CAT_ZONE: zone_name,
                    ALERT_CAT_ETA: eta,
                    ALERT_CAT_REASON: {
                        "zones": geofence_etas,
                        "rule": rule.name,
                    },
                }
                alerter.alert(meta, payload)
                if self.store is not None:
                    self.store.record_alert(plane, meta, payload)

    def _get_alerter(self, method: str, arguments: dict) -> Alerter:
        key = (method, str(sorted(arguments.items())))
        if key not in self._alerters:
            self._alerters[key] = create_alerter(method, arguments)
        return self._alerters[key]


def _lookup_callsign_hexdb(icao: str) -> str | None:
    try:
        resp = requests.get(f"https://hexdb.io/api/v1/aircraft/{icao}", timeout=1)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    data = resp.json()
    return data.get("Registration")
