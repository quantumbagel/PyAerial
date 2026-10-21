"""Flight, alert, and telemetry queries for the web portal."""

from __future__ import annotations

import inspect
import logging
import time
from typing import Any

from pyaerial.api.payloads import (
    alert_stats_by_flight,
    enrich_flight_detail,
    enrich_flight_summary,
    flight_summary,
    format_active_alerts,
    format_alert,
    merge_text_fields,
    telemetry_point,
)
from pyaerial.constants import LIVE_ENGINE_TTL_SECONDS
from pyaerial.enrich.aircraft_db import AircraftDB
from pyaerial.store.history import HistoryStore, HistoryUnavailable
from pyaerial.store.live import LiveStore
from pyaerial.store.redis_live import LiveUnavailable

log = logging.getLogger("pyaerial.webapp")

_MAX_Q = 80
#: Hard cap on telemetry points returned by one history fetchTelemetry.
MAX_HISTORY_TELEMETRY_POINTS = 20_000


def _normalize_q(q: str | None) -> str | None:
    if not q:
        return None
    text = str(q).strip()[:_MAX_Q]
    return text or None


def _history_available(history: HistoryStore | None) -> bool:
    return bool(history is not None and history.ping())


def engine_is_live(live_store: Any | None, now: float | None = None) -> bool:
    """True when the tracking engine heartbeat is fresh.

    Cheap two-state check for per-request use. ``view.reset`` needs a third
    "unknown" state for an unreachable Redis and uses
    :func:`pyaerial.store.live.engine_is_live` instead.

    The single source of truth for "is the engine live" in the API layer:
    ``/ready``, the stats payload (``engine_live``), the broadcaster, and every
    live query use it. Leftover Redis flight keys after a crash must not look
    like live traffic.
    """
    if live_store is None:
        return False
    live_fn = getattr(live_store, "engine_is_live", None)
    if callable(live_fn):
        try:
            return bool(live_fn())
        except Exception as exc:
            log.debug("engine_is_live check failed: %r", exc)
            return False
    getter = getattr(live_store, "engine_seen_at", None)
    if not callable(getter):
        return False
    try:
        seen = getter()
    except Exception as exc:
        log.debug("engine_seen_at check failed: %r", exc)
        return False
    if not isinstance(seen, (int, float)) or isinstance(seen, bool):
        return False
    stamp = time.time() if now is None else now
    return stamp - seen < LIVE_ENGINE_TTL_SECONDS


def _require_history(history: HistoryStore | None) -> HistoryStore | None:
    """Return the archive, or raise if it is configured but unreadable.

    ``None`` means no archive is attached (empty result is correct). A store
    that fails ``ping()`` must not look like an empty page.
    """
    if history is None:
        return None
    if not history.ping():
        raise HistoryUnavailable("history archive is unavailable")
    return history


def get_live_flights(
    live_store: LiveStore | None, aircraft_db: AircraftDB | None
) -> list[dict[str, Any]]:
    if live_store is None or not engine_is_live(live_store):
        return []
    return [
        enrich_flight_summary(summary, aircraft_db)
        for summary in live_store.get_flights()
    ]


def get_history_flights(
    history: HistoryStore | None,
    aircraft_db: AircraftDB | None,
    *,
    skip: int = 0,
    limit: int = 50,
    q: str | None = None,
    since: float | None = None,
    until: float | None = None,
) -> list[dict[str, Any]]:
    history = _require_history(history)
    if history is None:
        return []
    skip = max(0, skip)
    limit = min(max(limit, 1), 200)
    selected_docs = history.list_flights(
        skip=skip,
        limit=limit,
        q=_normalize_q(q),
        since=since,
        until=until,
    )
    if not selected_docs:
        return []

    selected_ids = [doc["_id"] for doc in selected_docs]
    flight_ends = {
        doc["_id"]: doc.get("end_time") or doc.get("start_time") or 0
        for doc in selected_docs
    }
    alert_stats = alert_stats_by_flight(
        history.alerts_for_flights(selected_ids),
        selected_ids,
        flight_ends=flight_ends,
    )
    latest_telemetry = history.latest_telemetry(selected_ids)

    return [
        flight_summary(
            {**doc, "alert_stats": alert_stats.get(doc["_id"])},
            latest_telemetry.get(doc["_id"]),
            aircraft_db,
        )
        for doc in selected_docs
    ]


def get_live_alerts(
    live_store: LiveStore | None,
    *,
    since: float = 0.0,
    flight_id: str | None = None,
    rule: str | None = None,
    limit: int = 0,
    skip: int = 0,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    if live_store is None or not engine_is_live(live_store):
        return []
    alerts = live_store.get_alerts(
        since=since,
        flight_id=flight_id,
        rule=rule,
        active_only=active_only,
    )
    if skip:
        alerts = alerts[skip:]
    if limit:
        alerts = alerts[:limit]
    return [format_alert(alert) for alert in alerts]


def get_tracked_live_alerts(
    live_store: LiveStore,
    flights: list[dict[str, Any]],
    *,
    limit: int = 0,
) -> list[dict[str, Any]]:
    flight_ids = {
        flight_id for flight in flights if (flight_id := flight.get("flight_id"))
    }
    if not flight_ids:
        return []
    alerts = get_live_alerts(live_store, active_only=False)
    filtered = [alert for alert in alerts if alert.get("flight_id") in flight_ids]
    filtered.sort(key=lambda alert: alert.get("activated_at") or 0, reverse=True)
    if limit:
        active = [
            alert
            for alert in filtered
            if alert.get("active", True) and not alert.get("deactivated_at")
        ]
        rest = [alert for alert in filtered if alert not in active]
        filtered = active + rest
        filtered = filtered[: max(limit, len(active))]
    return filtered


def get_flight_detail(
    flight_id: str,
    view: str,
    *,
    live_store: LiveStore | None,
    history: HistoryStore | None,
    aircraft_db: AircraftDB | None,
) -> dict[str, Any] | None:
    if view == "live":
        if live_store is None or not engine_is_live(live_store):
            return None
        flight_data = live_store.get_flight(flight_id)
        if not flight_data:
            return None
        telemetry = live_store.get_telemetry(flight_id)
        if telemetry:
            last = telemetry_point(telemetry[-1])
            flight_data.update(
                {
                    "latitude": last.get("latitude"),
                    "longitude": last.get("longitude"),
                    "altitude": last.get("altitude"),
                    "speed": last.get("speed"),
                    "heading": last.get("heading"),
                    "timestamp": last.get("timestamp"),
                }
            )
        return enrich_flight_detail(
            flight_data, flight_data.get("icao", ""), aircraft_db
        )

    history = _require_history(history)
    if history is None:
        return None
    doc = history.get_flight(flight_id)
    if not doc:
        return None
    icao = doc.get("icao", "")
    info = doc.get("info") or {}
    flight_end = doc.get("end_time") or doc.get("start_time") or 0
    alert_stats = alert_stats_by_flight(
        history.alerts_for_flights([flight_id]),
        [flight_id],
        flight_ends={flight_id: flight_end},
    ).get(flight_id)
    last = history.latest_telemetry([flight_id]).get(flight_id) or {}
    last_point = telemetry_point(last) if last else {}
    return enrich_flight_detail(
        {
            "flight_id": doc["_id"],
            "icao": icao,
            "active_alerts": format_active_alerts(doc),
            "alert_stats": alert_stats,
            "start_time": doc.get("start_time"),
            "end_time": doc.get("end_time"),
            "latitude": last_point.get("latitude"),
            "longitude": last_point.get("longitude"),
            "altitude": last_point.get("altitude"),
            "speed": last_point.get("speed"),
            "heading": last_point.get("heading"),
            "timestamp": last_point.get("timestamp") or flight_end,
            **merge_text_fields(doc, info),
            "is_live": False,
            "status": doc.get("status", "completed"),
        },
        icao,
        aircraft_db,
    )


def get_telemetry(
    flight_id: str,
    view: str,
    since: float,
    *,
    live_store: LiveStore | None,
    history: HistoryStore | None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if view == "live":
        if live_store is None or not engine_is_live(live_store):
            return []
        return [
            {**telemetry_point(doc), "flight_id": flight_id}
            for doc in live_store.get_telemetry(flight_id, since=since)
        ]
    history = _require_history(history)
    if history is None:
        return []
    # HistoryStore.get_telemetry has no LIMIT yet; pass one when it grows the
    # kwarg, and always truncate so one request cannot return an entire archive.
    cap = MAX_HISTORY_TELEMETRY_POINTS
    if limit:
        cap = max(1, min(int(limit), MAX_HISTORY_TELEMETRY_POINTS))
    if _accepts_kwarg(history.get_telemetry, "limit"):
        docs = history.get_telemetry(flight_id, since=since, limit=cap)
    else:
        docs = history.get_telemetry(flight_id, since=since)
    return [
        {**telemetry_point(doc), "flight_id": flight_id} for doc in docs[:cap]
    ]


def _accepts_kwarg(func: Any, name: str) -> bool:
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


def get_alerts(
    view: str,
    *,
    since: float = 0.0,
    until: float | None = None,
    flight_id: str | None = None,
    rule: str | None = None,
    q: str | None = None,
    limit: int = 0,
    skip: int = 0,
    live_store: LiveStore | None,
    history: HistoryStore | None,
    active_only: bool | None = None,
) -> list[dict[str, Any]]:
    if view == "live":
        if live_store is None or not engine_is_live(live_store):
            return []
        resolved_active_only = active_only if active_only is not None else not flight_id
        return get_live_alerts(
            live_store,
            since=since,
            flight_id=flight_id,
            rule=rule,
            limit=limit,
            skip=skip,
            active_only=resolved_active_only,
        )
    history = _require_history(history)
    if history is None:
        return []
    return [
        format_alert(doc)
        for doc in history.get_alerts(
            since=since,
            until=until,
            flight_id=flight_id,
            rule=rule,
            q=_normalize_q(q),
            limit=limit,
            skip=skip,
        )
    ]


def get_stats(
    live_store: LiveStore | None,
    history: HistoryStore | None,
) -> dict[str, Any]:
    live_flights = 0
    active_alerts = 0
    redis_ok = bool(live_store.ping()) if live_store is not None else False
    engine_live = engine_is_live(live_store)
    if live_store is not None:
        try:
            if engine_live:
                live_flights = live_store.count_flights()
                active_alerts = len(live_store.get_alerts(active_only=True))
        except LiveUnavailable:
            redis_ok = False
    engine_seen_at = None
    getter = getattr(live_store, "engine_seen_at", None) if live_store else None
    if callable(getter):
        try:
            engine_seen_at = getter()
        except Exception as exc:
            log.debug("engine_seen_at read failed: %r", exc)
            engine_seen_at = None
    retained_flights = 0
    historical_alerts = 0
    history_ok = _history_available(history)
    if history_ok and history is not None:
        retained_flights = history.count_flights()
        historical_alerts = history.count_alerts()
    return {
        "live_flights": live_flights,
        "active_alerts": active_alerts,
        "retained_flights": retained_flights,
        "historical_alerts": historical_alerts,
        "redis": redis_ok,
        "history": history_ok,
        "engine_seen_at": engine_seen_at,
        "engine_live": engine_live,
    }
