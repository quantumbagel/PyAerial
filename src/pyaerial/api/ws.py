"""WebSocket request dispatch for the web portal."""

from __future__ import annotations

import math
from typing import Any

from pyaerial.api.payloads import app_config_payload, view_param, zones_payload
from pyaerial.api.queries import (
    MAX_HISTORY_TELEMETRY_POINTS,
    get_alerts,
    get_flight_detail,
    get_history_flights,
    get_live_flights,
    get_stats,
    get_telemetry,
)
from pyaerial.enrich.aircraft_db import AircraftDB
from pyaerial.config.schema import Config
from pyaerial.store.history import HistoryStore
from pyaerial.store.live import LiveStore

_MAX_LIMIT = 200
_MAX_SKIP = 10_000
_MAX_ID_LEN = 128
_MAX_TEXT_LEN = 128


class BadRequest(ValueError):
    """A client request with missing or malformed parameters."""


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, number))


def _float_param(params: dict[str, Any], name: str, default: float | None) -> float | None:
    value = params.get(name)
    if value is None:
        return default
    if isinstance(value, bool):
        raise BadRequest(f"{name} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BadRequest(f"{name} must be a number") from exc
    if not math.isfinite(number):
        raise BadRequest(f"{name} must be finite")
    return number


def _text_param(params: dict[str, Any], name: str) -> str | None:
    value = params.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise BadRequest(f"{name} must be a string")
    return str(value)[:_MAX_TEXT_LEN]


def _flight_id_param(params: dict[str, Any]) -> str:
    flight_id = _text_param(params, "flightId")
    if not flight_id:
        raise BadRequest("Missing flightId")
    if len(flight_id) > _MAX_ID_LEN:
        raise BadRequest("flightId is too long")
    return flight_id


def handle_ws_request(
    action: str,
    params: dict[str, Any],
    *,
    config: Config,
    history: HistoryStore | None,
    live_store: LiveStore | None,
    aircraft_db: AircraftDB | None,
) -> Any:
    if not isinstance(params, dict):
        raise BadRequest("params must be an object")
    raw_view = params.get("view", "live")
    view = view_param(raw_view if isinstance(raw_view, str) else "live")
    if action == "fetchFlights":
        if view == "live":
            return get_live_flights(live_store, aircraft_db)
        return get_history_flights(
            history,
            aircraft_db,
            skip=_clamp_int(params.get("skip"), 0, 0, _MAX_SKIP),
            limit=_clamp_int(params.get("limit"), 50, 1, _MAX_LIMIT),
            q=_text_param(params, "q"),
            since=_float_param(params, "since", None),
            until=_float_param(params, "until", None),
        )

    if action == "fetchFlight":
        return get_flight_detail(
            _flight_id_param(params),
            view,
            live_store=live_store,
            history=history,
            aircraft_db=aircraft_db,
        )

    if action == "fetchTelemetry":
        return get_telemetry(
            _flight_id_param(params),
            view,
            _float_param(params, "since", 0.0) or 0.0,
            live_store=live_store,
            history=history,
            limit=_clamp_int(
                params.get("limit"),
                MAX_HISTORY_TELEMETRY_POINTS,
                1,
                MAX_HISTORY_TELEMETRY_POINTS,
            ),
        )

    if action == "fetchAlerts":
        since = _float_param(params, "since", 0.0) or 0.0
        until = _float_param(params, "until", None)
        flight_id = _text_param(params, "flightId")
        rule = _text_param(params, "rule")
        q = _text_param(params, "q")
        limit = _clamp_int(params.get("limit"), 0, 0, _MAX_LIMIT)
        if view != "live" and limit == 0:
            limit = _MAX_LIMIT
        skip = _clamp_int(params.get("skip"), 0, 0, _MAX_SKIP)
        active_only_val = params.get("active_only")
        if active_only_val is None:
            active_only = None
        elif isinstance(active_only_val, str):
            active_only = active_only_val.strip().lower() in {"1", "true", "yes", "on"}
        else:
            active_only = bool(active_only_val)
        return get_alerts(
            view,
            since=since,
            until=until,
            flight_id=flight_id,
            rule=rule,
            q=q,
            limit=limit,
            skip=skip,
            live_store=live_store,
            history=history,
            active_only=active_only,
        )

    if action == "fetchStats":
        return get_stats(live_store, history)

    if action == "fetchZones":
        return zones_payload(config)

    if action == "fetchConfig":
        return app_config_payload(config)

    raise BadRequest("Unknown action")
