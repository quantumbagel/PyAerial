"""Geofence alert lifecycle: match, hysteresis, activate / while_active / deactivate."""

from __future__ import annotations

import logging
import math
import time
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from shapely import Polygon

from pyaerial.alerters import Alerter, create_alerter
from pyaerial.alerts.dispatch import SHUTDOWN_GRACE_SECONDS, AlertDispatcher
from pyaerial.calc import evaluate, geo
from pyaerial.calc.motion import ResolvedMotion
from pyaerial.config.schema import AlertActionConfig, Config, RuleConfig, ZoneConfig
from pyaerial.constants import (
    ALERT_CAT_ETA,
    ALERT_CAT_REASON,
    ALERT_CAT_TYPE,
    ALERT_CAT_ZONE,
    STORE_ALT,
    STORE_CALLSIGN,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_LAT,
    STORE_LONG,
    STORE_VERT_SPEED,
)
from pyaerial.models import Plane, latest_datum
from pyaerial.store.live import EpisodeStore

log = logging.getLogger("pyaerial.alerts")

_ETA_HORIZON = 10_000
# Slack on the early-out below: the bounding-box distance is a lower bound up
# to rounding, so only skip zones clearly out of reach.
_ETA_REACH_MARGIN = 1.05
_ETA_REACH_SLACK_KM = 0.05
_ALERT_HOOK_ACTIVATE = "activate"
_ALERT_HOOK_DEACTIVATE = "deactivate"
_ALERT_HOOK_WHILE_ACTIVE = "while_active"
# reason.ended_by on deactivate notifications.
ENDED_CLEARED = "cleared"  # the rule's conditions stopped holding
ENDED_LOST = "lost"  # aircraft not heard for tracking.remember_planes
ENDED_SHUTDOWN = "shutdown"  # engine stopping while the aircraft was tracked
# Left active by an engine that stopped without shutting down (crash, kill).
# Its flight is closed as interrupted at the next start and tracking starts a
# new flight, so the episode cannot resume: it is closed, and an aircraft
# still in the zone activates again under a new alert_id.
ENDED_RESTART = "restart"


def _rule_eta_bound(zone: ZoneConfig) -> float | None:
    """Largest ETA any rule in *zone* can accept; None if no rule reads eta.

    A rule with an eta ``min`` but no ``max`` accepts any large ETA (and inf),
    so it needs the full horizon.
    """
    bounds: list[float] = []
    for rule in zone.rules:
        for field, spec in rule.when.items():
            if evaluate.canonical_field(field) != ALERT_CAT_ETA:
                continue
            if spec.maximum is None:
                return float(_ETA_HORIZON)
            bounds.append(spec.maximum)
    if not bounds:
        return None
    return min(max(bounds), float(_ETA_HORIZON))


def _last_position(plane: Plane) -> tuple[float, float] | None:
    # No fix means no position: report None, never a fake (0, 0) in the
    # Gulf of Guinea.
    recv = plane.received_data
    lat_series = recv.get(STORE_LAT)
    lon_series = recv.get(STORE_LONG)
    if lat_series and lon_series:
        return (lat_series[-1].value, lon_series[-1].value)
    return None


def _plane_as_flight(plane: Plane, flight_id: str) -> Plane | None:
    """*plane* as seen by the stores under *flight_id* (``{icao}-{start}``)."""
    if plane.flight_id == flight_id:
        return plane
    icao, _, start = flight_id.rpartition("-")
    try:
        first_packet = float(int(start))
    except ValueError:
        return None
    if not icao:
        return None
    return replace(plane, track=replace(plane.track, first_packet=first_packet))


class AlertEngine:
    """Per-plane geofence matching and alerter dispatch."""

    def __init__(
        self,
        config: Config,
        polygons: dict[str, Polygon],
        store: EpisodeStore | None = None,
    ):
        self.config = config
        self.polygons = polygons
        self.store = store
        self._alerters: dict[tuple[str, str], Alerter] = {}
        # Keys whose construction failed: reported once at startup, then their
        # notifications are skipped instead of retried (and logged) per alert.
        self._broken_alerters: set[tuple[str, str]] = set()
        self._dispatcher = AlertDispatcher()
        # (icao, zone, rule) -> {activated_at (wall), last_periodic (monotonic), alert_id}
        self._alert_state: dict[tuple[str, str, str], dict] = {}
        # Hold / hysteresis start times use time.monotonic() so a wall-clock
        # step (NTP, manual change) cannot fire or suppress alerts.
        self._pending_match: dict[tuple[str, str, str], float] = {}
        self._pending_unmatch: dict[tuple[str, str, str], float] = {}
        # icao -> flight_id last passed to check(): the flight that owns the
        # pending matches and alert state of that ICAO.
        self._live_flight: dict[str, str] = {}
        self._eta_bounds = {
            name: _rule_eta_bound(zone) for name, zone in config.zones.items()
        }
        self._build_alerters()

    def _build_alerters(self) -> None:
        """Create every configured alerter now so a broken one shows at startup."""
        for zone_name, zone in self.config.zones.items():
            for rule in zone.rules:
                actions = [*rule.on_activate, *rule.on_deactivate]
                if rule.while_active is not None:
                    actions.extend(rule.while_active.actions)
                for action in actions:
                    key = self._alerter_key(action.method, action.options)
                    if key in self._alerters or key in self._broken_alerters:
                        continue
                    try:
                        self._alerters[key] = create_alerter(
                            action.method, action.options
                        )
                    except Exception as exc:
                        self._broken_alerters.add(key)
                        # Message only: options (webhook URLs) are secrets.
                        log.error(
                            "zones.%s.rules.%s: alerter %r is unusable (%s: %s); "
                            "its notifications will be dropped, other alerters "
                            "are unaffected",
                            zone_name,
                            rule.name,
                            action.method,
                            type(exc).__name__,
                            exc,
                        )

    @staticmethod
    def _alerter_key(method: str, arguments: dict) -> tuple[str, str]:
        return (method, str(sorted(arguments.items())))

    def close(self, timeout: float = SHUTDOWN_GRACE_SECONDS) -> None:
        self._dispatcher.close(timeout=timeout)
        for alerter in self._alerters.values():
            try:
                alerter.close()
            except Exception:
                log.exception("Alerter %s failed to close", alerter.method)
        self._alerters.clear()
        self._alert_state.clear()
        self._pending_match.clear()
        self._pending_unmatch.clear()

    def check(
        self,
        plane: Plane,
        position: tuple[float, float],
        motion: ResolvedMotion,
        callsign: str,
    ) -> None:
        icao = plane.icao
        flight_id = plane.flight_id
        previous_flight = self._live_flight.get(icao)
        if previous_flight is not None and previous_flight != flight_id:
            # Same aircraft, new flight id without an explicit handover():
            # continue its alerts rather than re-notifying.
            self.handover(plane, previous_flight, flight_id)
        self._live_flight[icao] = flight_id
        now = time.time()
        mono = time.monotonic()
        matching: dict[tuple[str, str, str], tuple[str, RuleConfig]] = {}

        eta_speed = motion.speed_kph
        eta_heading = motion.heading_deg
        turn_rate = motion.turn_rate_deg_s

        use_curved = self.config.tracking.curved_projection

        # Full-horizon ETA per zone, computed only when an alert reports it
        # (active alert entries, notifications): identical to computing it
        # for every zone up front, which cost a geodesic projection per
        # aircraft x zone x tick.
        exact_etas: dict[str, float] = {}

        def zone_eta(zone_name: str) -> float:
            if zone_name not in exact_etas:
                exact_etas[zone_name] = self._zone_eta(
                    self.polygons[zone_name], position, motion, use_curved
                )
            return exact_etas[zone_name]

        all_etas_cache: dict[str, float] = {}

        def all_etas() -> dict[str, float]:
            if not all_etas_cache:
                all_etas_cache.update({z: zone_eta(z) for z in self.config.zones})
            return all_etas_cache

        for zone_name, zone in self.config.zones.items():
            polygon = self.polygons[zone_name]
            bound = self._eta_bounds.get(zone_name, _ETA_HORIZON)
            if bound is None:
                eta = math.inf  # no rule in this zone reads eta
            elif eta_speed > 0 and geo._bbox_min_distance_km(polygon, position) > (
                bound * eta_speed / 3600 * _ETA_REACH_MARGIN + _ETA_REACH_SLACK_KM
            ):
                # Cannot reach the zone within any rule's eta max: the exact
                # ETA is larger (or inf), so every eta rule fails either way.
                eta = math.inf
            else:
                eta = zone_eta(zone_name)

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
                matching[key] = (zone_name, rule)

        active_alerts: list[dict] = []
        for key, (zone_name, rule) in matching.items():
            self._pending_unmatch.pop(key, None)
            state = self._alert_state.get(key)
            if state is None:
                first_match = self._pending_match.setdefault(key, mono)
                if mono - first_match < rule.activation_hold_seconds():
                    continue
                self._pending_match.pop(key, None)
            eta = zone_eta(zone_name)
            geofence_etas = all_etas()
            if state is None:
                alert_id = f"{flight_id}:{zone_name}:{rule.name}:{uuid.uuid4().hex}"
                state = {
                    "activated_at": now,
                    "last_periodic": mono,
                    "alert_id": alert_id,
                    "flight_id": flight_id,
                    "eta": eta,
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
                state["eta"] = eta
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
                    if mono - state["last_periodic"] >= interval:
                        state["last_periodic"] = mono
                        self._on_while_active(
                            plane,
                            zone_name,
                            rule,
                            eta,
                            geofence_etas,
                            position,
                            callsign,
                            state["alert_id"],
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
            stale_rule = self._find_rule(zone_name, rule_name)
            hyst = stale_rule.hysteresis_seconds if stale_rule is not None else 0
            geofence_etas = all_etas()
            last_eta = geofence_etas.get(zone_name, state.get("eta"))
            first_unmatch = self._pending_unmatch.setdefault(key, mono)
            if mono - first_unmatch < (hyst or 0):
                active_alerts.append(
                    {
                        "alert_id": state["alert_id"],
                        "zone": zone_name,
                        "rule": rule_name,
                        "activated_at": state["activated_at"],
                        "eta": last_eta,
                    }
                )
                continue
            self._pending_unmatch.pop(key, None)
            self._alert_state.pop(key, None)
            if stale_rule is not None:
                self._on_deactivate(
                    plane,
                    zone_name,
                    stale_rule,
                    last_eta,
                    geofence_etas,
                    position,
                    callsign,
                    now,
                    state["alert_id"],
                    state["activated_at"],
                    ended_by=ENDED_CLEARED,
                )
            elif self.store is not None:
                self.store.record_alert_episode(
                    plane,
                    {
                        STORE_ICAO: icao,
                        STORE_CALLSIGN: callsign,
                        ALERT_CAT_TYPE: rule_name,
                        ALERT_CAT_ZONE: zone_name,
                        ALERT_CAT_ETA: last_eta,
                    },
                    self._build_payload(plane, position),
                    alert_id=state["alert_id"],
                    activated_at=state["activated_at"],
                    deactivated_at=now,
                    active=False,
                )

        plane.active_alerts = active_alerts

    def _zone_eta(
        self,
        polygon: Polygon,
        position: tuple[float, float],
        motion: ResolvedMotion,
        use_curved: bool,
    ) -> float:
        if use_curved and abs(motion.turn_rate_deg_s) >= 0.1:
            return geo.time_to_enter_geofence_curved(
                position,
                motion.heading_deg,
                motion.speed_kph,
                motion.turn_rate_deg_s,
                polygon,
                _ETA_HORIZON,
            )
        return geo.time_to_enter_geofence(
            position, motion.heading_deg, motion.speed_kph, polygon, _ETA_HORIZON
        )

    def handover(self, plane: Plane, old_flight_id: str, new_flight_id: str) -> None:
        """Continue an aircraft's active alerts from one flight id to the next.

        For flight segmentation (``tracking.max_flight_seconds``): the same
        aircraft, still tracked, is archived as segment *old_flight_id* and
        goes on as *new_flight_id*. *plane* is the live plane (either id).

        Contract:

        * No notifications: the aircraft never left the zone, so webhook
          consumers keep one activate and one deactivate per episode.
        * The episode keeps its ``alert_id`` and ``activated_at``: alert_id is
          the pairing key consumers already hold, and the archive's
          ``alerts`` table is keyed by alert_id alone. It is re-recorded under
          *new_flight_id*: its one archive row (and live record) moves to the
          new flight, keeping the segment where it began (start_flight_id).
        * Archive: the old segment's retention still counts the episode (the
          engine passes it along); the archive ends with one row per
          episode, on the segment where it ended, with the original
          ``activated_at``.
        * Dwell / hysteresis timers and the while_active cadence carry over.
        * Idempotent. If the engine skips it, the first ``check()`` that sees
          the new flight id for this ICAO calls it.
        """
        icao = plane.icao
        if not icao or old_flight_id == new_flight_id:
            return
        self._live_flight[icao] = new_flight_id
        new_view = _plane_as_flight(plane, new_flight_id) or plane
        payload: dict | None = None
        for key, state in self._alert_state.items():
            if key[0] != icao or state.get("flight_id", old_flight_id) != old_flight_id:
                continue
            state["flight_id"] = new_flight_id
            rule = self._find_rule(key[1], key[2])
            if self.store is None or rule is None:
                continue
            if payload is None:
                payload = self._build_payload(plane, _last_position(plane))
            meta = self._build_meta(
                new_view,
                key[1],
                rule,
                state.get("eta"),
                {},
                plane.info.callsign or "",
                _ALERT_HOOK_WHILE_ACTIVE,
                state["alert_id"],
            )
            self.store.record_alert_episode(
                new_view,
                meta,
                payload,
                alert_id=state["alert_id"],
                activated_at=state["activated_at"],
                active=True,
            )

    def deactivate(self, plane: Plane, ended_by: str | None = None) -> None:
        """Close every alert of a flight being removed, with one deactivate each.

        Covers the alerts this process raised (a previous process's are closed
        by :meth:`close_restored`).
        ``ended_by`` defaults to ``lost`` for a plane silent for longer than
        ``remember_planes`` (expiry) and ``shutdown`` otherwise: the engine
        only removes a still-fresh plane when it is stopping. The engine
        calls it only for the aircraft's tracked flight, so every alert state
        and pending match of the ICAO goes.
        """
        icao = plane.icao
        self._live_flight.pop(icao, None)
        self._pending_match = {
            key: ts for key, ts in self._pending_match.items() if key[0] != icao
        }
        self._pending_unmatch = {
            key: ts for key, ts in self._pending_unmatch.items() if key[0] != icao
        }
        stale_keys = [k for k in self._alert_state if k[0] == icao]
        now = time.time()
        if ended_by is None:
            silent = (
                now - plane.track.last_update > self.config.tracking.remember_planes
            )
            ended_by = ENDED_LOST if silent else ENDED_SHUTDOWN
        callsign = plane.info.callsign or ""
        position = _last_position(plane)

        geofence_etas = {z: None for z in self.config.zones}
        for key in stale_keys:
            zone_name, rule_name = key[1], key[2]
            state = self._alert_state.pop(key)
            rule = self._find_rule(zone_name, rule_name)
            last_eta = state.get("eta")
            if rule is not None:
                self._on_deactivate(
                    plane,
                    zone_name,
                    rule,
                    last_eta,
                    geofence_etas,
                    position,
                    callsign,
                    now,
                    state["alert_id"],
                    state["activated_at"],
                    ended_by=ended_by,
                )
            elif self.store is not None:
                self.store.record_alert_episode(
                    plane,
                    {
                        STORE_ICAO: icao,
                        STORE_CALLSIGN: callsign,
                        ALERT_CAT_TYPE: rule_name,
                        ALERT_CAT_ZONE: zone_name,
                        ALERT_CAT_ETA: last_eta,
                    },
                    self._build_payload(plane, position),
                    alert_id=state["alert_id"],
                    activated_at=state["activated_at"],
                    deactivated_at=now,
                    active=False,
                )
        plane.active_alerts = []

    def _find_rule(self, zone_name: str, rule_name: str) -> RuleConfig | None:
        zone = self.config.zones.get(zone_name)
        if zone is None:
            return None
        return next((r for r in zone.rules if r.name == rule_name), None)

    def close_restored(
        self, plane: Plane, doc: Mapping[str, Any], *, now: float | None = None
    ) -> None:
        """Close an episode a previous engine process left open (crash, kill).

        Sends its one deactivate notification (``ended_by: restart``), then
        records it closed. It ends when it was last known to hold (its last
        refresh), not at the (possibly much later) restart. *plane* is the
        episode's flight as archived (``plane.flight_id`` is the episode's);
        *doc* its archived row. No notification when the rule is no longer
        configured.
        """
        if self.store is None:
            return
        now = time.time() if now is None else now
        alert_id = str(doc.get("alert_id") or "")
        zone_name = str(doc.get("zone") or "")
        rule_name = str(doc.get("rule") or "")
        activated_at = doc.get("activated_at")
        if not isinstance(activated_at, (int, float)):
            activated_at = now
        last_seen = doc.get("last_updated")
        ended_at = (
            min(now, max(activated_at, last_seen))
            if isinstance(last_seen, (int, float))
            else now
        )
        eta = doc.get("eta") if isinstance(doc.get("eta"), (int, float)) else None
        lat, lon = doc.get("latitude"), doc.get("longitude")
        position = (
            (lat, lon)
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float))
            else _last_position(plane)
        )
        payload = self._build_payload(plane, position)
        callsign = str(doc.get(STORE_CALLSIGN) or plane.info.callsign or "")
        rule = self._find_rule(zone_name, rule_name)
        if rule is None:
            log.warning(
                "Closing alert %s left active by a previous engine run; "
                "zones.%s.rules.%s is no longer configured, so no "
                "deactivate notification is sent",
                alert_id,
                zone_name,
                rule_name,
            )
            meta: dict[str, Any] = {
                STORE_ICAO: str(doc.get(STORE_ICAO) or plane.icao),
                STORE_CALLSIGN: callsign,
                ALERT_CAT_TYPE: rule_name,
                ALERT_CAT_ZONE: zone_name,
                ALERT_CAT_ETA: eta,
                ALERT_CAT_REASON: {
                    "hook": _ALERT_HOOK_DEACTIVATE,
                    "ended_by": ENDED_RESTART,
                },
            }
        else:
            log.info("Closing alert %s left active by a previous engine run", alert_id)
            meta = self._build_meta(
                plane,
                zone_name,
                rule,
                eta,
                {},
                callsign,
                _ALERT_HOOK_DEACTIVATE,
                alert_id,
                ended_by=ENDED_RESTART,
                activated_at=activated_at,
                deactivated_at=ended_at,
            )
            if rule.on_deactivate:
                self._run_actions(rule.on_deactivate, meta, payload, alert_id)
        self.store.record_alert_episode(
            plane,
            meta,
            payload,
            alert_id=alert_id,
            activated_at=activated_at,
            deactivated_at=ended_at,
            active=False,
        )

    def _build_meta(
        self,
        plane: Plane,
        zone_name: str,
        rule: RuleConfig,
        eta: float | None,
        geofence_etas: Mapping[str, float | None],
        callsign: str,
        hook: str,
        alert_id: str,
        *,
        ended_by: str | None = None,
        activated_at: float | None = None,
        deactivated_at: float | None = None,
    ) -> dict:
        """Alert description handed to alerters (and, by named keys, the store).

        The webhook's ``pyaerial.alert/1`` body is built from this; see
        :mod:`pyaerial.alerters.contract` for every field.
        """
        info = plane.info
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
        finite_etas = {
            name: (
                value
                if isinstance(value, (int, float)) and math.isfinite(value)
                else None
            )
            for name, value in geofence_etas.items()
        }
        finite_eta = (
            eta if isinstance(eta, (int, float)) and math.isfinite(eta) else None
        )
        meta: dict[str, Any] = {
            # One alert_id per episode: lets webhook consumers pair the
            # activate / while_active / deactivate notifications. The live
            # store copies named keys only, so its documents are unchanged.
            "alert_id": alert_id,
            "flight_id": plane.flight_id,
            STORE_ICAO: info.icao,
            STORE_CALLSIGN: callsign,
            ALERT_CAT_TYPE: rule.name,
            ALERT_CAT_ZONE: zone_name,
            ALERT_CAT_ETA: finite_eta,
            "color": color,
            "timestamp": time.time(),
            "activated_at": (
                activated_at if activated_at is not None else self._activated_at(alert_id)
            ),
            "deactivated_at": deactivated_at,
            ALERT_CAT_REASON: {
                "zones": finite_etas,
                "rule": rule.name,
                "hook": hook,
            },
        }
        if ended_by is not None:
            meta[ALERT_CAT_REASON]["ended_by"] = ended_by
        for field, val in (
            ("registration", info.registration),
            ("manufacturer", info.manufacturer),
            ("manufacturer_name", info.manufacturer_name),
            ("model", info.model),
            ("owner", info.owner),
            ("aircraft_type", info.aircraft_type),
            ("photo_url", info.photo_url),
            ("country", info.country),
            ("operator_callsign", info.operator_callsign),
        ):
            if val:
                meta[field] = val
        return meta

    def _activated_at(self, alert_id: str) -> float | None:
        for state in self._alert_state.values():
            if state.get("alert_id") == alert_id:
                return state.get("activated_at")
        return None

    def _build_payload(self, plane: Plane, position: tuple[float, float] | None) -> dict:
        # Baro altitude, else geometric GNSS height (TC 20-22 only aircraft);
        # altitude_source tells consumers which one they got.
        alt, alt_source = evaluate.altitude_m(plane)
        speed = latest_datum(plane.calculated_data, STORE_HORIZ_SPEED)
        heading = latest_datum(plane.calculated_data, STORE_HEADING)
        vert_speed = latest_datum(plane.received_data, STORE_VERT_SPEED)
        return {
            STORE_LAT: position[0] if position is not None else None,
            STORE_LONG: position[1] if position is not None else None,
            STORE_ALT: alt,
            "altitude_source": alt_source,
            STORE_HORIZ_SPEED: speed.value if speed else None,
            STORE_HEADING: heading.value if heading else None,
            STORE_VERT_SPEED: vert_speed.value if vert_speed else None,
        }

    def _run_actions(
        self,
        actions: list[AlertActionConfig],
        meta: dict,
        payload: dict,
        alert_id: str,
    ) -> None:
        for action in actions:
            alerter = self._get_alerter(action.method, action.options)
            if alerter is None:
                continue
            # Keyed by alert id: one alert's hooks are delivered in order.
            self._dispatcher.submit(alert_id, self._safe_alert, alerter, meta, payload)

    @staticmethod
    def _safe_alert(alerter: Alerter, meta: dict, payload: dict) -> None:
        try:
            alerter.alert(meta, payload)
        except Exception:
            log.exception("Alerter %s failed", getattr(alerter, "method", alerter))

    def _on_activate(
        self,
        plane: Plane,
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
            plane,
            zone_name,
            rule,
            eta,
            geofence_etas,
            callsign,
            _ALERT_HOOK_ACTIVATE,
            alert_id,
            activated_at=now,
        )
        payload = self._build_payload(plane, position)
        if rule.on_activate:
            self._run_actions(rule.on_activate, meta, payload, alert_id)
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
        plane: Plane,
        zone_name: str,
        rule: RuleConfig,
        eta: float | None,
        geofence_etas: Mapping[str, float | None],
        position: tuple[float, float] | None,
        callsign: str,
        now: float,
        alert_id: str,
        activated_at: float,
        *,
        ended_by: str,
    ) -> None:
        meta = self._build_meta(
            plane,
            zone_name,
            rule,
            eta,
            geofence_etas,
            callsign,
            _ALERT_HOOK_DEACTIVATE,
            alert_id,
            ended_by=ended_by,
            activated_at=activated_at,
            deactivated_at=now,
        )
        payload = self._build_payload(plane, position)
        if rule.on_deactivate:
            self._run_actions(rule.on_deactivate, meta, payload, alert_id)
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
        plane: Plane,
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
            alert_id,
        )
        payload = self._build_payload(plane, position)
        self.store.update_active_alert(plane, alert_id, meta, payload, now)

    def _on_while_active(
        self,
        plane: Plane,
        zone_name: str,
        rule: RuleConfig,
        eta: float,
        geofence_etas: dict[str, float],
        position: tuple[float, float],
        callsign: str,
        alert_id: str,
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
            alert_id,
        )
        payload = self._build_payload(plane, position)
        self._run_actions(while_active.actions, meta, payload, alert_id)

    def _get_alerter(self, method: str, arguments: dict) -> Alerter | None:
        key = self._alerter_key(method, arguments)
        if key in self._broken_alerters:
            return None
        if key not in self._alerters:
            # Only reached for rules added after construction (e.g. tests).
            try:
                self._alerters[key] = create_alerter(method, arguments)
            except Exception as exc:
                self._broken_alerters.add(key)
                log.error(
                    "Alerter %r is unusable (%s: %s); its notifications will be "
                    "dropped",
                    method,
                    type(exc).__name__,
                    exc,
                )
                return None
        return self._alerters[key]
