"""Archive rows as documents, shared by the reader (portal) and the writer
(startup recovery): JSON columns decoded, a damaged value dropped rather
than failing the whole read."""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from typing import Any

log = logging.getLogger("pyaerial.store")

# Telemetry columns stored as JSON arrays.
_JSON_LIST_COLUMNS = ("receivers", "integrity_flags")


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def flight_from_row(row: sqlite3.Row) -> dict[str, Any]:
    raw = _row_dict(row)
    try:
        info = json.loads(raw.get("info") or "{}")
    except json.JSONDecodeError:
        info = {}
    if not isinstance(info, dict):
        info = {}
    return {
        "_id": raw["flight_id"],
        "flight_id": raw["flight_id"],
        "icao": raw["icao"],
        "status": raw["status"],
        "start_time": raw["start_time"],
        "end_time": raw["end_time"],
        "retained": bool(raw["retained"]),
        "callsign": raw["callsign"] or "",
        "model": raw["model"] or "",
        "owner": raw["owner"] or "",
        "country": raw["country"] or "",
        "aircraft_type": raw["aircraft_type"] or "",
        "registration": raw.get("registration") or "",
        "info": info,
        "active_alerts": [],
    }


def telemetry_from_row(row: sqlite3.Row) -> dict[str, Any]:
    doc = {key: value for key, value in _row_dict(row).items() if value is not None}
    for key in _JSON_LIST_COLUMNS:
        if key not in doc:
            continue
        try:
            value = json.loads(doc[key])
        except (json.JSONDecodeError, TypeError):
            value = None
        if isinstance(value, list):
            doc[key] = value
            continue
        # A hand-edited or damaged row: drop the field, keep the sample.
        log.warning(
            "Ignoring malformed %s in telemetry %s at %s",
            key,
            doc.get("flight_id"),
            doc.get("timestamp"),
        )
        del doc[key]
    return doc


def alert_from_row(row: sqlite3.Row) -> dict[str, Any]:
    raw = _row_dict(row)
    reason = raw["reason"]
    if isinstance(reason, str) and reason[:1] in "{[":
        with contextlib.suppress(json.JSONDecodeError):
            reason = json.loads(reason)
    return {
        "alert_id": raw["alert_id"],
        "flight_id": raw["flight_id"],
        "icao": raw["icao"],
        "callsign": raw["callsign"],
        "zone": raw["zone"],
        "rule": raw["rule"],
        "active": bool(raw["active"]),
        "activated_at": raw["activated_at"],
        "deactivated_at": raw["deactivated_at"],
        "eta": raw["eta"],
        "reason": reason,
        "last_updated": raw["last_updated"],
        "latitude": raw["latitude"],
        "longitude": raw["longitude"],
        "altitude": raw["altitude"],
    }
