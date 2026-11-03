"""Redis queries shared by the live reader and writer.

Functions over a client snapshot; they raise :class:`redis.RedisError` and
leave logging, reconnects and fallbacks to the caller.
"""

from __future__ import annotations

from typing import Any, cast

import redis

from pyaerial.store.live.keys import (
    KEY_ACTIVE_ALERTS,
    KEY_ALERT_EPISODES,
    KEY_ALERTS,
    KEY_FLIGHT,
    KEY_FLIGHTS,
    KEY_TELEMETRY,
    safe_json_loads,
)
from pyaerial.store.present import live_flight_detail, live_flight_summary

# Telemetry point fields the /ws/live push carries only when present.
_LIVE_POINT_OPTIONAL = (
    "gnss_height",
    "receivers",
    "integrity_score",
    "integrity_status",
    "integrity_flags",
)


def newest_first(flights: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flights.sort(key=lambda item: item.get("start_time") or 0, reverse=True)
    return flights


def read_flight_summaries(client: redis.Redis) -> list[dict[str, Any]]:
    """Every live flight's summary, in ``live:flights`` order (unsorted by time)."""
    results: list[dict[str, Any]] = []
    flight_ids = sorted(client.smembers(KEY_FLIGHTS))
    if flight_ids:
        # One round trip for every flight doc and its newest telemetry
        # point (was 2 round trips per flight).
        pipe = client.pipeline(transaction=False)
        for flight_id in flight_ids:
            pipe.get(KEY_FLIGHT.format(flight_id=flight_id))
            pipe.zrevrange(KEY_TELEMETRY.format(flight_id=flight_id), 0, 0)
        replies = pipe.execute()
        for index in range(len(flight_ids)):
            raw, last_raw = replies[2 * index], replies[2 * index + 1]
            flight = safe_json_loads(raw) if raw else None
            if not isinstance(flight, dict):
                continue
            last_tel = safe_json_loads(last_raw[0]) if last_raw else None
            results.append(
                live_flight_summary(
                    flight, last_tel if isinstance(last_tel, dict) else None
                )
            )
    return results


def read_flight(client: redis.Redis, flight_id: str) -> dict[str, Any] | None:
    raw = client.get(KEY_FLIGHT.format(flight_id=flight_id))
    if not raw:
        return None
    doc = safe_json_loads(raw)
    if not isinstance(doc, dict):
        return None
    return live_flight_detail(doc, flight_id)


def read_telemetry(
    client: redis.Redis,
    flight_id: str,
    since: float,
    *,
    limit: int | None = None,
    newest: bool = False,
) -> list[dict[str, Any]]:
    key = KEY_TELEMETRY.format(flight_id=flight_id)
    low = f"({since}" if since > 0 else "-inf"
    if limit is None:
        if since > 0:
            raw_points = client.zrangebyscore(key, low, "+inf")
        else:
            raw_points = client.zrange(key, 0, -1)
    elif not newest:
        raw_points = client.zrangebyscore(key, low, "+inf", start=0, num=limit)
    elif since > 0:
        raw_points = client.zrevrangebyscore(key, "+inf", low, start=0, num=limit)
        raw_points.reverse()
    else:
        raw_points = client.zrange(key, -limit, -1)
    points = []
    for point in raw_points:
        parsed = safe_json_loads(point)
        if isinstance(parsed, dict):
            points.append(parsed)
    return points


def read_latest_telemetry(client: redis.Redis, flight_id: str) -> dict[str, Any] | None:
    """The flight's newest telemetry point (one ZREVRANGE), or None."""
    raw = client.zrevrange(KEY_TELEMETRY.format(flight_id=flight_id), 0, 0)
    parsed = safe_json_loads(raw[0]) if raw else None
    return parsed if isinstance(parsed, dict) else None


def live_point(flight_id: str, point: dict[str, Any]) -> dict[str, Any]:
    live = {
        "flight_id": flight_id,
        "icao": point.get("icao"),
        "timestamp": point.get("timestamp"),
        "latitude": point.get("latitude"),
        "longitude": point.get("longitude"),
        "altitude": point.get("altitude"),
        "speed": point.get("speed"),
        "heading": point.get("heading"),
    }
    # Optional fields, copied only when present: GNSS height (TC 20-22
    # positions) and the provenance WEBSOCKET.md promises on the push.
    for key in _LIVE_POINT_OPTIONAL:
        if point.get(key) is not None:
            live[key] = point[key]
    return live


def collect_live_telemetry(
    client: redis.Redis, since: float, into: list[dict[str, Any]]
) -> None:
    """Append every live flight's points after *since* to *into*.

    Appends as it reads, so a caller keeps what was read before an error.
    """
    for flight_id in cast("set[str]", client.smembers(KEY_FLIGHTS)):
        for point in read_telemetry(client, flight_id, since):
            into.append(live_point(flight_id, point))


def by_time(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    points.sort(key=lambda item: item.get("timestamp") or 0)
    return points


def read_alerts(
    client: redis.Redis, flight_id: str | None, active_only: bool
) -> list[dict[str, Any]]:
    if active_only:
        raw_alerts = client.hvals(KEY_ACTIVE_ALERTS)
    elif flight_id:
        raw_alerts = client.hvals(KEY_ALERTS.format(flight_id=flight_id))
    else:
        raw_alerts = client.hvals(KEY_ALERT_EPISODES)
    alerts = []
    for raw in raw_alerts:
        parsed = safe_json_loads(raw)
        if not isinstance(parsed, dict):
            continue
        if active_only and flight_id and parsed.get("flight_id") != flight_id:
            continue
        alerts.append(parsed)
    return alerts


def alerts_by_id(docs: list[dict[str, Any]]) -> dict[Any, dict[str, Any]]:
    return {doc.get("alert_id") or id(doc): doc for doc in docs}


def filter_alerts(
    alerts: list[dict[str, Any]],
    *,
    since: float,
    rule: str | None,
    active_only: bool,
) -> list[dict[str, Any]]:
    """Apply the ``get_alerts`` filters; newest activation first."""
    if since:
        alerts = [
            a
            for a in alerts
            if (a.get("activated_at") or a.get("last_updated") or 0) >= since
        ]
    if rule:
        alerts = [a for a in alerts if a.get("rule") == rule]
    if active_only:
        alerts = [a for a in alerts if a.get("active", True)]
    alerts.sort(
        key=lambda item: item.get("activated_at") or item.get("last_updated") or 0,
        reverse=True,
    )
    return alerts
