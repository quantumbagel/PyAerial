"""Shape live-store documents into portal/CLI flight summaries."""

from __future__ import annotations

from typing import Any


def live_flight_detail(doc: dict[str, Any], flight_id: str) -> dict[str, Any]:
    info = doc.get("info", {})
    return {
        "flight_id": flight_id,
        "icao": doc.get("icao", ""),
        "active_alerts": doc.get("active_alerts", []),
        "start_time": doc.get("start_time"),
        "end_time": doc.get("end_time"),
        "callsign": doc.get("callsign") or info.get("callsign"),
        "model": doc.get("model") or info.get("model"),
        "owner": doc.get("owner") or info.get("owner"),
        "country": doc.get("country") or info.get("country"),
        "aircraft_type": doc.get("aircraft_type") or info.get("aircraft_type"),
        "is_live": True,
        "status": "live",
    }


def live_flight_summary(
    doc: dict[str, Any], last_tel: dict[str, Any] | None
) -> dict[str, Any]:
    info = doc.get("info", {})
    lat = lon = alt = speed = heading = timestamp = None
    if last_tel:
        lat = last_tel.get("latitude")
        lon = last_tel.get("longitude")
        alt = last_tel.get("altitude")
        speed = last_tel.get("speed")
        heading = last_tel.get("heading")
        timestamp = last_tel.get("timestamp")
    flight_id = doc.get("flight_id") or doc.get("_id")
    return {
        "flight_id": flight_id,
        "icao": doc.get("icao", ""),
        "active_alerts": doc.get("active_alerts", []),
        "start_time": doc.get("start_time"),
        "end_time": doc.get("end_time"),
        "callsign": doc.get("callsign") or info.get("callsign"),
        "model": doc.get("model") or info.get("model"),
        "owner": doc.get("owner") or info.get("owner"),
        "country": doc.get("country") or info.get("country"),
        "aircraft_type": doc.get("aircraft_type") or info.get("aircraft_type"),
        "latitude": lat,
        "longitude": lon,
        "altitude": alt,
        "speed": speed,
        "heading": heading,
        "is_live": True,
        "status": "live",
        "retained": False,
        "timestamp": timestamp,
    }
