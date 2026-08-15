"""WebSocket request dispatch for the web portal."""

from __future__ import annotations

from typing import Any

import pymongo

from pyaerial.api.payloads import app_config_payload, view_param, zones_payload
from pyaerial.api.protocol import LiveStore
from pyaerial.api.queries import (
    get_alerts,
    get_flight_detail,
    get_history_flights,
    get_live_flights,
    get_stats,
    get_telemetry,
)
from pyaerial.calc.aircraft_db import AircraftDB
from pyaerial.config.schema import Config


def _flight_id_param(params: dict[str, Any]) -> str:
    flight_id = params.get("flightId")
    if not flight_id:
        raise ValueError("Missing flightId")
    return str(flight_id)


def handle_ws_request(
    action: str,
    params: dict[str, Any],
    *,
    config: Config,
    db: pymongo.database.Database | None,
    live_store: LiveStore | None,
    aircraft_db: AircraftDB | None,
) -> Any:
    view = view_param(params.get("view", "live"))
    if action == "fetchFlights":
        if view == "live":
            return get_live_flights(live_store, aircraft_db)
        return get_history_flights(db, aircraft_db)

    if action == "fetchFlight":
        return get_flight_detail(
            _flight_id_param(params),
            view,
            live_store=live_store,
            db=db,
            aircraft_db=aircraft_db,
        )

    if action == "fetchTelemetry":
        since_val = params.get("since")
        since = float(since_val) if since_val is not None else 0.0
        return get_telemetry(
            _flight_id_param(params),
            view,
            since,
            live_store=live_store,
            db=db,
        )

    if action == "fetchAlerts":
        since_val = params.get("since")
        since = float(since_val) if since_val is not None else 0.0
        flight_id = params.get("flightId")
        rule = params.get("rule")
        limit_val = params.get("limit")
        limit = int(limit_val) if limit_val is not None else 0
        skip_val = params.get("skip")
        skip = int(skip_val) if skip_val is not None else 0
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
            flight_id=flight_id,
            rule=rule,
            limit=limit,
            skip=skip,
            live_store=live_store,
            db=db,
            active_only=active_only,
        )

    if action == "fetchStats":
        return get_stats(live_store, db, aircraft_db)

    if action == "fetchZones":
        return zones_payload(config)

    if action == "fetchConfig":
        return app_config_payload(config)

    raise ValueError(f"Unknown action: {action}")
