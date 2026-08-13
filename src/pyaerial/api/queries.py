"""Flight, alert, and telemetry queries for the web portal."""
from __future__ import annotations

from typing import Any

import pymongo

from pyaerial.api.payloads import (
    alert_stats_by_flight,
    enrich_flight_detail,
    enrich_flight_summary,
    enrich_from_aircraft_db,
    flight_summary,
    format_active_alerts,
    format_alert,
    telemetry_point,
)
from pyaerial.api.protocol import LiveStore
from pyaerial.calc.aircraft_db import AircraftDB

_FLIGHT_STATUS_LIVE = "live"


def get_live_flights(live_store: LiveStore, aircraft_db: AircraftDB | None) -> list[dict[str, Any]]:
    return [
        enrich_flight_summary(summary, aircraft_db)
        for summary in live_store.get_flights()
    ]


def get_history_flights(db: pymongo.database.Database, aircraft_db: AircraftDB | None) -> list[dict[str, Any]]:
    flights_col = db.get_collection("flights")
    telemetry_col = db.get_collection("telemetry")
    alerts_col = db.get_collection("alerts")

    completed_cursor = flights_col.find({
        "status": {"$ne": _FLIGHT_STATUS_LIVE},
        "$or": [{"retained": True}, {"retained": {"$exists": False}}],
    }).sort("end_time", -1).limit(100)

    completed_docs = list(completed_cursor)
    if not completed_docs:
        return []

    flight_ids = [doc["_id"] for doc in completed_docs]
    alert_flight_ids = {
        doc["_id"]
        for doc in alerts_col.aggregate([
            {"$match": {"flight_id": {"$in": flight_ids}}},
            {"$group": {"_id": "$flight_id"}},
        ])
    }

    selected_docs = []
    for doc in completed_docs:
        if doc.get("retained") or doc["_id"] in alert_flight_ids:
            selected_docs.append(doc)
        if len(selected_docs) >= 50:
            break

    if not selected_docs:
        return []

    selected_ids = [doc["_id"] for doc in selected_docs]
    flight_ends = {doc["_id"]: doc.get("end_time") or doc.get("start_time") or 0 for doc in selected_docs}
    alert_stats = alert_stats_by_flight(db, selected_ids, flight_ends=flight_ends)
    latest_telemetry = {
        doc["_id"]: doc["doc"]
        for doc in telemetry_col.aggregate([
            {"$match": {"flight_id": {"$in": selected_ids}}},
            {"$sort": {"timestamp": -1}},
            {"$group": {
                "_id": "$flight_id",
                "doc": {"$first": "$$ROOT"},
            }},
        ])
    }

    return [
        flight_summary(
            {**doc, "alert_stats": alert_stats.get(doc["_id"])},
            latest_telemetry.get(doc["_id"]),
            aircraft_db,
        )
        for doc in selected_docs
    ]


def get_live_alerts(live_store: LiveStore, *, since: float = 0.0,
                    flight_id: str | None = None, rule: str | None = None,
                    limit: int = 0, skip: int = 0,
                    active_only: bool = True) -> list[dict[str, Any]]:
    alerts = live_store.get_alerts(
        since=since, flight_id=flight_id, rule=rule, active_only=active_only,
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
    flight_ids = {flight_id for flight in flights if (flight_id := flight.get("flight_id"))}
    if not flight_ids:
        return []
    alerts = get_live_alerts(live_store, active_only=False)
    filtered = [alert for alert in alerts if alert.get("flight_id") in flight_ids]
    filtered.sort(key=lambda alert: alert.get("activated_at") or 0, reverse=True)
    if limit:
        filtered = filtered[:limit]
    return filtered


def get_flight_detail(
    flight_id: str,
    view: str,
    *,
    live_store: LiveStore,
    db: pymongo.database.Database,
    aircraft_db: AircraftDB | None,
) -> dict[str, Any] | None:
    if view == "live":
        flight_data = live_store.get_flight(flight_id)
        if not flight_data:
            return None
        telemetry = live_store.get_telemetry(flight_id)
        if telemetry:
            last = telemetry_point(telemetry[-1])
            flight_data.update({
                "latitude": last.get("latitude"),
                "longitude": last.get("longitude"),
                "altitude": last.get("altitude"),
                "speed": last.get("speed"),
                "heading": last.get("heading"),
                "timestamp": last.get("timestamp"),
            })
        return enrich_flight_detail(flight_data, flight_data.get("icao", ""), aircraft_db)

    doc = db.get_collection("flights").find_one({"_id": flight_id})
    if not doc:
        return None
    icao = doc.get("icao", "")
    enriched = enrich_from_aircraft_db(icao, aircraft_db)
    info = doc.get("info", {})
    flight_end = doc.get("end_time") or doc.get("start_time") or 0
    alert_stats = alert_stats_by_flight(
        db, [flight_id], flight_ends={flight_id: flight_end},
    ).get(flight_id)
    return enrich_flight_detail({
        "flight_id": doc["_id"],
        "icao": icao,
        "active_alerts": format_active_alerts(doc),
        "alert_stats": alert_stats,
        "start_time": doc.get("start_time"),
        "end_time": doc.get("end_time"),
        "callsign": doc.get("callsign") or info.get("callsign") or enriched.get("callsign"),
        "model": doc.get("model") or info.get("model") or enriched.get("model"),
        "owner": doc.get("owner") or info.get("owner") or enriched.get("owner"),
        "country": doc.get("country") or info.get("country") or enriched.get("country"),
        "aircraft_type": doc.get("aircraft_type") or info.get("aircraft_type") or enriched.get("aircraft_type"),
        "registration": doc.get("registration") or info.get("registration") or enriched.get("registration"),
        "is_live": False,
        "status": doc.get("status", "completed"),
    }, icao, aircraft_db)


def get_telemetry(
    flight_id: str,
    view: str,
    since: float,
    *,
    live_store: LiveStore,
    db: pymongo.database.Database,
) -> list[dict[str, Any]]:
    if view == "live":
        return live_store.get_telemetry(flight_id, since=since)
    filt: dict[str, Any] = {"flight_id": flight_id}
    if since > 0:
        filt["timestamp"] = {"$gt": since}
    cursor = db.get_collection("telemetry").find(filt).sort("timestamp", 1)
    return [telemetry_point(doc) for doc in cursor]


def get_alerts(
    view: str,
    *,
    since: float = 0.0,
    flight_id: str | None = None,
    rule: str | None = None,
    limit: int = 0,
    skip: int = 0,
    live_store: LiveStore,
    db: pymongo.database.Database,
    active_only: bool | None = None,
) -> list[dict[str, Any]]:
    if view == "live":
        resolved_active_only = active_only if active_only is not None else not flight_id
        return get_live_alerts(
            live_store, since=since, flight_id=flight_id, rule=rule,
            limit=limit, skip=skip, active_only=resolved_active_only,
        )
    filt: dict[str, Any] = {}
    if since:
        filt["activated_at"] = {"$gt": since}
    if flight_id:
        filt["flight_id"] = flight_id
    if rule:
        filt["rule"] = rule
    cursor = db.get_collection("alerts").find(filt).sort("activated_at", -1)
    if skip:
        cursor = cursor.skip(skip)
    if limit:
        cursor = cursor.limit(limit)
    return [format_alert(doc) for doc in cursor]


def get_stats(
    live_store: LiveStore | None,
    db: pymongo.database.Database | None,
    aircraft_db: AircraftDB | None,
) -> dict[str, int]:
    live_flights = len(get_live_flights(live_store, aircraft_db)) if live_store else 0
    active_alerts = len(get_live_alerts(live_store, active_only=True)) if live_store else 0
    retained_flights = 0
    historical_alerts = 0
    if db is not None:
        retained_flights = db.get_collection("flights").count_documents({
            "status": {"$ne": _FLIGHT_STATUS_LIVE},
            "$or": [{"retained": True}, {"retained": {"$exists": False}}],
        })
        historical_alerts = db.get_collection("alerts").count_documents({})
    return {
        "live_flights": live_flights,
        "active_alerts": active_alerts,
        "retained_flights": retained_flights,
        "historical_alerts": historical_alerts,
    }
