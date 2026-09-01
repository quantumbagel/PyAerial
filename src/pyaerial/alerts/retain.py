"""Decide whether a completed flight is interesting enough to keep in Mongo."""

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
            valid = _count_valid_ticks(
                plane,
                polygon,
                rule.when,
                first_time,
                last_time,
            )
            if valid >= rule.dwell_seconds:
                return True
    return False


def _count_valid_ticks(
    plane: dict,
    polygon: Polygon,
    when: dict,
    first_time: float,
    last_time: float,
) -> int:
    lat_series = plane.get(STORE_RECV_DATA, {}).get(STORE_LAT, [])
    if not lat_series:
        return 0
    samples = [
        datum for datum in lat_series if first_time <= datum.time <= last_time
    ]
    if len(samples) > 3600:
        step = max(1, len(samples) // 1800)
        samples = samples[::step]
    valid = 0
    for lat in samples:
        lon = get_latest(STORE_RECV_DATA, STORE_LONG, plane, lat.time)
        heading = get_latest(STORE_CALC_DATA, STORE_HEADING, plane, lat.time)
        speed = get_latest(STORE_CALC_DATA, STORE_HORIZ_SPEED, plane, lat.time)
        if None in (lon, heading, speed):
            continue
        position = (lat.value, lon.value)
        eta = geo.time_to_enter_geofence(
            position, heading.value, speed.value, polygon, _ETA_HORIZON
        )
        resolver = evaluate.make_resolver(plane, eta, polygon, position, lat.time)
        if evaluate.when_passes(when, resolver):
            valid += 1
    return valid
