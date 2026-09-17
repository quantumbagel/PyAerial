"""Decide whether a completed flight is interesting enough to keep in history."""

from __future__ import annotations

from typing import Any

from shapely import Polygon

from pyaerial.calc import evaluate, geo
from pyaerial.config.schema import Config
from pyaerial.constants import (
    STORE_CALC_DATA,
    STORE_FIRST_PACKET,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_MOST_RECENT_PACKET,
    STORE_RECV_DATA,
)
from pyaerial.models import get_latest

_ETA_HORIZON = 10_000


def should_retain(
    plane: dict,
    alerts: list[dict[str, Any]],
    config: Config,
    polygons: dict[str, Polygon],
) -> bool:
    """Return True if this flight should be written to historical storage."""
    rules_by_key: dict[tuple[str, str], Any] = {}
    for zone_name, zone in config.zones.items():
        for rule in zone.rules:
            rules_by_key[(zone_name, rule.name)] = rule

    for alert in alerts:
        rule = rules_by_key.get((alert.get("zone", ""), alert.get("rule", "")))
        if rule is None or not rule.retain:
            continue
        activated = alert.get("activated_at")
        if activated is None:
            continue
        deactivated = alert.get("deactivated_at")
        if deactivated is None:
            deactivated = plane.get(STORE_INTERNAL, {}).get(
                STORE_MOST_RECENT_PACKET, activated
            )
        if (deactivated - activated) >= rule.dwell_seconds:
            return True

    recv = plane.get(STORE_RECV_DATA, {})
    calc = plane.get(STORE_CALC_DATA, {})
    if STORE_LAT not in recv or STORE_HEADING not in calc:
        return False

    internal = plane[STORE_INTERNAL]
    first_time = internal[STORE_FIRST_PACKET]
    last_time = internal[STORE_MOST_RECENT_PACKET]

    for zone_name, zone in config.zones.items():
        if not any(rule.retain for rule in zone.rules):
            continue
        polygon = polygons.get(zone_name)
        if polygon is None:
            continue
        for rule in zone.rules:
            if not rule.retain:
                continue
            matched = _matching_seconds(
                plane,
                polygon,
                rule.when,
                first_time,
                last_time,
            )
            if matched >= rule.dwell_seconds:
                return True
    return False


def _matching_seconds(
    plane: dict,
    polygon: Polygon,
    when: dict,
    first_time: float,
    last_time: float,
) -> float:
    """Wall-clock seconds during which ``when`` held, using sample timestamps."""
    lat_series = plane.get(STORE_RECV_DATA, {}).get(STORE_LAT, [])
    if not lat_series:
        return 0.0
    samples = [
        datum for datum in lat_series if first_time <= datum.time <= last_time
    ]
    matched_seconds = 0.0
    prev_time: float | None = None
    prev_match = False
    for lat in samples:
        lon = get_latest(STORE_RECV_DATA, STORE_LONG, plane, lat.time)
        heading = get_latest(STORE_CALC_DATA, STORE_HEADING, plane, lat.time)
        speed = get_latest(STORE_CALC_DATA, STORE_HORIZ_SPEED, plane, lat.time)
        if None in (lon, heading, speed):
            prev_match = False
            prev_time = lat.time
            continue
        position = (lat.value, lon.value)
        eta = geo.time_to_enter_geofence(
            position, heading.value, speed.value, polygon, _ETA_HORIZON
        )
        resolver = evaluate.make_resolver(plane, eta, polygon, position, lat.time)
        matched = evaluate.when_passes(when, resolver)
        if matched and prev_match and prev_time is not None:
            matched_seconds += max(0.0, lat.time - prev_time)
        prev_match = matched
        prev_time = lat.time
    if prev_match and prev_time is not None:
        matched_seconds += max(0.0, last_time - prev_time)
    if matched_seconds == 0.0 and samples and prev_match:
        matched_seconds = max(0.0, last_time - first_time)
    return matched_seconds
