"""Geofence alert lifecycle: match, hysteresis, activate / while_active / deactivate."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import math
import time

from shapely import Polygon

from pyaerial.alerters import Alerter, create_alerter
from pyaerial.calc import evaluate, geo
from pyaerial.calc.motion import ResolvedMotion
from pyaerial.config.schema import AlertActionConfig, Config, RuleConfig
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
    STORE_VERT_SPEED,
)
from pyaerial.models import flight_id_for_plane, get_latest
from pyaerial.store.redis_live import RedisLiveStore

log = logging.getLogger("pyaerial.alerts")

_ETA_HORIZON = 10_000
_ALERT_HOOK_ACTIVATE = "activate"
_ALERT_HOOK_DEACTIVATE = "deactivate"
_ALERT_HOOK_WHILE_ACTIVE = "while_active"


class AlertEngine:
    """Per-plane geofence matching and alerter dispatch."""

    def __init__(
        self,
        config: Config,
        polygons: dict[str, Polygon],
        store: RedisLiveStore | None = None,
    ):
        self.config = config
        self.polygons = polygons
        self.store = store
        self._alerters: dict[tuple[str, str], Alerter] = {}
        self._alert_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="alert-dispatch"
        )
        # (icao, zone, rule) -> {activated_at, last_periodic, alert_id}
        self._alert_state: dict[tuple[str, str, str], dict] = {}
        self._pending_match: dict[tuple[str, str, str], float] = {}
        self._pending_unmatch: dict[tuple[str, str, str], float] = {}

    def close(self) -> None:
        for alerter in self._alerters.values():
            alerter.close()
        self._alerters.clear()
        self._alert_executor.shutdown(wait=True, cancel_futures=True)
        self._alert_state.clear()
        self._pending_match.clear()
        self._pending_unmatch.clear()

    def check(
        self,
        plane: dict,
        position: tuple[float, float],
        motion: ResolvedMotion,
        callsign: str,
    ) -> None:
        icao = plane[STORE_INFO][STORE_ICAO].lower()
        flight_id = flight_id_for_plane(plane)
        now = time.time()
        geofence_etas: dict[str, float] = {}
        matching: dict[tuple[str, str, str], tuple[str, RuleConfig, float]] = {}

        eta_speed = motion.speed_kph
        eta_heading = motion.heading_deg
        turn_rate = motion.turn_rate_deg_s

        use_curved = self.config.tracking.curved_projection

        for zone_name, zone in self.config.zones.items():
            polygon = self.polygons[zone_name]

            if use_curved and abs(turn_rate) >= 0.1:
                eta = geo.time_to_enter_geofence_curved(
                    position,
                    eta_heading,
                    eta_speed,
                    turn_rate,
                    polygon,
                    _ETA_HORIZON,
                )
            else:
                eta = geo.time_to_enter_geofence(
                    position,
                    eta_heading,
                    eta_speed,
                    polygon,
                    _ETA_HORIZON,
                )
            geofence_etas[zone_name] = eta

            resolver = evaluate.make_resolver(plane, eta, polygon, position)
            for rule in zone.rules:
                if rule.predict_seconds is not None and rule.predict_seconds > 0:
                    pred_resolver = evaluate.make_predicted_resolver(
                        plane,
                        polygon,
                        position,
                        eta_heading,
                        eta_speed,
                        turn_rate,
                        rule.predict_seconds,
                        curved=use_curved,
                    )
                    if not (
                        evaluate.when_passes(rule.when, resolver)
                        or evaluate.when_passes(rule.when, pred_resolver)
                    ):
                        continue
                else:
                    if not evaluate.when_passes(rule.when, resolver):
                        continue
                key = (icao, zone_name, rule.name)
                matching[key] = (zone_name, rule, eta)

        active_alerts: list[dict] = []
        for key, (zone_name, rule, eta) in matching.items():
            self._pending_unmatch.pop(key, None)
            state = self._alert_state.get(key)
            if state is None:
                first_match = self._pending_match.setdefault(key, now)
                if now - first_match < (rule.hysteresis_seconds or 0):
                    continue
                self._pending_match.pop(key, None)
                alert_id = f"{flight_id}:{zone_name}:{rule.name}:{int(now)}"
                state = {
                    "activated_at": now,
                    "last_periodic": now,
                    "alert_id": alert_id,
                }
                self._alert_state[key] = state
                self._on_activate(
                    plane,
                    zone_name,
                    rule,
                    eta,
                    geofence_etas,
                    position,
                    callsign,
                    now,
                    alert_id,
                )
            else:
                self._refresh_active_alert(
                    plane,
                    zone_name,
                    rule,
                    eta,
                    geofence_etas,
                    position,
                    callsign,
                    now,
                    state["alert_id"],
                )
                if rule.while_active is not None:
                    interval = rule.while_active.interval_seconds
                    if now - state["last_periodic"] >= interval:
                        state["last_periodic"] = now
                        self._on_while_active(
                            plane,
                            zone_name,
                            rule,
                            eta,
                            geofence_etas,
                            position,
                            callsign,
                        )

            active_alerts.append(
                {
                    "alert_id": state["alert_id"],
                    "zone": zone_name,
                    "rule": rule.name,
                    "activated_at": state["activated_at"],
                    "eta": eta,
                }
            )

        stale_keys = [
            key
            for key in list(self._alert_state) + list(self._pending_match)
            if key[0] == icao and key not in matching
        ]
        for key in stale_keys:
            self._pending_match.pop(key, None)
            state = self._alert_state.get(key)
            if state is None:
                continue
            zone_name, rule_name = key[1], key[2]
            zone = self.config.zones.get(zone_name)
            rule = (
                next((r for r in zone.rules if r.name == rule_name), None)
                if zone
                else None
            )
            hyst = rule.hysteresis_seconds if rule is not None else 0
            first_unmatch = self._pending_unmatch.setdefault(key, now)
            if now - first_unmatch < (hyst or 0):
                active_alerts.append(
                    {
                        "alert_id": state["alert_id"],
                        "zone": zone_name,
                        "rule": rule_name,
                        "activated_at": state["activated_at"],
                        "eta": geofence_etas.get(zone_name, math.inf),
                    }
                )
                continue
            self._pending_unmatch.pop(key, None)
            self._alert_state.pop(key, None)
            eta = geofence_etas.get(zone_name, math.inf)
            if rule is not None:
                self._on_deactivate(
                    plane,
                    zone_name,
                    rule,
                    eta,
                    geofence_etas,
                    position,
                    callsign,
                    now,
                    state["alert_id"],
                    state["activated_at"],
                )
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

    def deactivate(self, plane: dict) -> None:
        """Deactivate and clean up all active alerts for a plane being removed."""
        info = plane.get(STORE_INFO, {})
        if STORE_ICAO not in info:
            return
        icao = info[STORE_ICAO].lower()
        self._pending_match = {
            key: ts for key, ts in self._pending_match.items() if key[0] != icao
        }
        self._pending_unmatch = {
            key: ts for key, ts in self._pending_unmatch.items() if key[0] != icao
        }
        stale_keys = [k for k in self._alert_state if k[0] == icao]
        if not stale_keys:
            plane["active_alerts"] = []
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
            rule = (
                next((r for r in zone.rules if r.name == rule_name), None)
                if zone
                else None
            )
            if rule is not None:
                self._on_deactivate(
                    plane,
                    zone_name,
                    rule,
                    math.inf,
                    geofence_etas,
                    position,
                    callsign,
                    now,
                    state["alert_id"],
                    state["activated_at"],
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

    def _build_meta(
        self,
        plane: dict,
        zone_name: str,
        rule: RuleConfig,
        eta: float,
        geofence_etas: dict[str, float],
        callsign: str,
        hook: str,
    ) -> dict:
        info = plane.get(STORE_INFO, {})
        zone_cfg = (
            self.config.zones.get(zone_name)
            if self.config and self.config.zones
            else None
        )
        color = (
            rule.color
            or (zone_cfg.color if zone_cfg else None)
            or (self.config.alert_colors.get(rule.name) if self.config else None)
            or "#ef4444"
        )
        meta = {
            STORE_ICAO: info.get(STORE_ICAO, ""),
            STORE_CALLSIGN: callsign,
            ALERT_CAT_TYPE: rule.name,
            ALERT_CAT_ZONE: zone_name,
            ALERT_CAT_ETA: eta,
            "color": color,
            ALERT_CAT_REASON: {
                "zones": geofence_etas,
                "rule": rule.name,
                "hook": hook,
            },
        }
        for field in (
            "registration",
            "manufacturer",
            "manufacturer_name",
            "model",
            "owner",
            "aircraft_type",
            "photo_url",
            "country",
            "operator_callsign",
        ):
            val = info.get(field)
            if val:
                meta[field] = val
        return meta

    def _build_payload(self, plane: dict, position: tuple[float, float]) -> dict:
        alt = get_latest(STORE_RECV_DATA, STORE_ALT, plane)
        speed = get_latest(STORE_CALC_DATA, STORE_HORIZ_SPEED, plane)
        heading = get_latest(STORE_CALC_DATA, STORE_HEADING, plane)
        vert_speed = get_latest(STORE_RECV_DATA, STORE_VERT_SPEED, plane)
        return {
            STORE_LAT: position[0],
            STORE_LONG: position[1],
            STORE_ALT: alt.value if alt else None,
            STORE_HORIZ_SPEED: speed.value if speed else None,
            STORE_HEADING: heading.value if heading else None,
            STORE_VERT_SPEED: vert_speed.value if vert_speed else None,
        }

    def _run_actions(
        self, actions: list[AlertActionConfig], meta: dict, payload: dict
    ) -> None:
        for action in actions:
            alerter = self._get_alerter(action.method, action.options)
            if alerter is None:
                continue
            self._alert_executor.submit(self._safe_alert, alerter, meta, payload)

    @staticmethod
    def _safe_alert(alerter: Alerter, meta: dict, payload: dict) -> None:
        try:
            alerter.alert(meta, payload)
        except Exception:
            log.exception("Alerter %s failed", getattr(alerter, "method", alerter))

    def _on_activate(
        self,
        plane: dict,
        zone_name: str,
        rule: RuleConfig,
        eta: float,
        geofence_etas: dict[str, float],
        position: tuple[float, float],
        callsign: str,
        now: float,
        alert_id: str,
    ) -> None:
        meta = self._build_meta(
            plane, zone_name, rule, eta, geofence_etas, callsign, _ALERT_HOOK_ACTIVATE
        )
        payload = self._build_payload(plane, position)
        if rule.on_activate:
            self._run_actions(rule.on_activate, meta, payload)
        if self.store is not None:
            self.store.record_alert_episode(
                plane,
                meta,
                payload,
                alert_id=alert_id,
                activated_at=now,
                active=True,
            )

    def _on_deactivate(
        self,
        plane: dict,
        zone_name: str,
        rule: RuleConfig,
        eta: float,
        geofence_etas: dict[str, float],
        position: tuple[float, float],
        callsign: str,
        now: float,
        alert_id: str,
        activated_at: float,
    ) -> None:
        meta = self._build_meta(
            plane, zone_name, rule, eta, geofence_etas, callsign, _ALERT_HOOK_DEACTIVATE
        )
        payload = self._build_payload(plane, position)
        if rule.on_deactivate:
            self._run_actions(rule.on_deactivate, meta, payload)
        if self.store is not None:
            self.store.record_alert_episode(
                plane,
                meta,
                payload,
                alert_id=alert_id,
                activated_at=activated_at,
                deactivated_at=now,
                active=False,
            )

    def _refresh_active_alert(
        self,
        plane: dict,
        zone_name: str,
        rule: RuleConfig,
        eta: float,
        geofence_etas: dict[str, float],
        position: tuple[float, float],
        callsign: str,
        now: float,
        alert_id: str,
    ) -> None:
        if self.store is None:
            return
        meta = self._build_meta(
            plane,
            zone_name,
            rule,
            eta,
            geofence_etas,
            callsign,
            _ALERT_HOOK_WHILE_ACTIVE,
        )
        payload = self._build_payload(plane, position)
        self.store.update_active_alert(plane, alert_id, meta, payload, now)

    def _on_while_active(
        self,
        plane: dict,
        zone_name: str,
        rule: RuleConfig,
        eta: float,
        geofence_etas: dict[str, float],
        position: tuple[float, float],
        callsign: str,
    ) -> None:
        while_active = rule.while_active
        if while_active is None or not while_active.actions:
            return
        meta = self._build_meta(
            plane,
            zone_name,
            rule,
            eta,
            geofence_etas,
            callsign,
            _ALERT_HOOK_WHILE_ACTIVE,
        )
        payload = self._build_payload(plane, position)
        self._run_actions(while_active.actions, meta, payload)

    def _get_alerter(self, method: str, arguments: dict) -> Alerter | None:
        key = (method, str(sorted(arguments.items())))
        if key not in self._alerters:
            try:
                self._alerters[key] = create_alerter(method, arguments)
            except Exception:
                log.exception("Could not create alerter %r; alert dropped", method)
                return None
        return self._alerters[key]
