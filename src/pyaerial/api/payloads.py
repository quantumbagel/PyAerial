"""Payload formatting and enrichment for the web portal."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Any

import pymongo

from pyaerial.enrich.aircraft_db import AircraftDB, normalize_photo_url
from pyaerial.config.schema import Config

FLIGHT_STATUS_LIVE = "live"


def zones_payload(config: Config) -> dict[str, Any]:
    zones = []
    for name, zone in config.zones.items():
        rules = []
        for rule in zone.rules:
            when: dict[str, dict[str, float]] = {}
            for field_name, constraint in rule.when.items():
                entry: dict[str, float] = {}
                if constraint.minimum is not None:
                    entry["min"] = constraint.minimum
                if constraint.maximum is not None:
                    entry["max"] = constraint.maximum
                when[field_name] = entry
            rule_payload: dict[str, Any] = {
                "name": rule.name,
                "when": when,
                "dwell_seconds": rule.dwell_seconds,
            }
            if rule.color is not None:
                rule_payload["color"] = rule.color
            rules.append(rule_payload)
        zone_payload: dict[str, Any] = {
            "name": name,
            "coordinates": zone.coordinates or [],
            "rules": rules,
        }
        if zone.color is not None:
            zone_payload["color"] = zone.color
        zones.append(zone_payload)
    return {
        "home": {
            "latitude": config.home.latitude,
            "longitude": config.home.longitude,
        },
        "zones": zones,
        "alert_colors": dict(config.alert_colors),
    }


def view_param(view: str) -> str:
    view = view.lower()
    return view if view in ("live", "history") else "live"


def enrich_from_aircraft_db(
    icao: str, aircraft_db: AircraftDB | None
) -> dict[str, str | None]:
    if not aircraft_db:
        return {}
    if aircraft_db.is_cached(icao):
        meta = aircraft_db.lookup_cached(icao)
    else:
        meta = aircraft_db.lookup_cached_fast(icao)
    if not meta:
        return {}
    return {
        "callsign": meta.get("callsign"),
        "model": meta.get("model"),
        "owner": meta.get("owner"),
        "country": meta.get("country"),
        "aircraft_type": meta.get("typecode"),
        "registration": meta.get("registration"),
        "photo_url": normalize_photo_url(meta.get("photo_url")),
        "photo_photographer": meta.get("photo_photographer"),
        "photo_link": meta.get("photo_link"),
    }


def telemetry_point(doc: dict[str, Any]) -> dict[str, Any]:
    point: dict[str, Any] = {
        "timestamp": doc["timestamp"],
        "altitude": doc.get("altitude"),
        "speed": doc.get("speed"),
        "heading": doc.get("heading"),
    }
    if "latitude" in doc and "longitude" in doc:
        point["latitude"] = doc["latitude"]
        point["longitude"] = doc["longitude"]
    elif "position" in doc:
        position = doc.get("position") or {}
        coords = position.get("coordinates") or [None, None]
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            point["longitude"] = coords[0]
            point["latitude"] = coords[1]
    return point


def sanitize_for_json(data: Any) -> Any:
    """Convert non-finite floats to null so responses are valid JSON."""
    if isinstance(data, float):
        return data if math.isfinite(data) else None
    if isinstance(data, dict):
        return {key: sanitize_for_json(value) for key, value in data.items()}
    if isinstance(data, list):
        return [sanitize_for_json(value) for value in data]
    if isinstance(data, tuple):
        return [sanitize_for_json(value) for value in data]
    return data


def _alert_coords(doc: dict[str, Any]) -> tuple[Any, Any]:
    position = doc.get("position") or {}
    coords = position.get("coordinates") or [None, None]
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None, None
    return coords[1], coords[0]


def format_alert(doc: dict[str, Any]) -> dict[str, Any]:
    latitude, longitude = _alert_coords(doc)
    alert_id = (
        doc.get("alert_id")
        or f"{doc.get('flight_id', '')}:{doc.get('zone', '')}:{doc.get('rule', '')}"
    )
    return {
        "alert_id": alert_id,
        "flight_id": doc.get("flight_id"),
        "icao": doc.get("icao"),
        "callsign": doc.get("callsign"),
        "zone": doc.get("zone"),
        "rule": doc.get("rule"),
        "active": doc.get("active", False),
        "activated_at": doc.get("activated_at"),
        "deactivated_at": doc.get("deactivated_at"),
        "eta": doc.get("eta"),
        "altitude": doc.get("altitude"),
        "latitude": latitude,
        "longitude": longitude,
    }


def format_active_alerts(doc: dict[str, Any]) -> list[dict[str, Any]]:
    alerts = doc.get("active_alerts") or []
    return [
        {
            "alert_id": item.get("alert_id", ""),
            "zone": item.get("zone", ""),
            "rule": item.get("rule", ""),
            "activated_at": item.get("activated_at"),
            "eta": item.get("eta"),
        }
        for item in alerts
    ]


def live_alert_stats(active_alerts: list[dict[str, Any]]) -> dict[str, int]:
    now = time.time()
    total = 0.0
    for item in active_alerts:
        activated = item.get("activated_at")
        if activated is not None:
            total += max(0.0, now - activated)
    return {
        "episode_count": len(active_alerts),
        "total_seconds": int(total),
        "active_count": len(active_alerts),
    }


def alert_stats_by_flight(
    db: pymongo.database.Database,
    flight_ids: list[str],
    *,
    flight_ends: dict[str, float] | None = None,
) -> dict[str, dict[str, int]]:
    if not flight_ids:
        return {}
    ends = flight_ends or {}
    episodes: dict[str, dict[str, dict[str, float | None]]] = defaultdict(dict)
    for doc in db.get_collection("alerts").find({"flight_id": {"$in": flight_ids}}):
        flight_id = doc.get("flight_id")
        alert_id = (
            doc.get("alert_id")
            or f"{flight_id}:{doc.get('zone', '')}:{doc.get('rule', '')}"
        )
        if not flight_id or not alert_id:
            continue
        bucket = episodes[flight_id].setdefault(
            alert_id,
            {
                "activated_at": None,
                "deactivated_at": None,
            },
        )
        activated = doc.get("activated_at")
        if activated is not None:
            current = bucket["activated_at"]
            bucket["activated_at"] = (
                activated if current is None else min(current, activated)
            )
        deactivated = doc.get("deactivated_at")
        if deactivated is not None:
            current = bucket["deactivated_at"]
            bucket["deactivated_at"] = (
                deactivated if current is None else max(current, deactivated)
            )

    stats: dict[str, dict[str, int]] = {}
    for flight_id, alert_map in episodes.items():
        total = 0.0
        for episode in alert_map.values():
            start = episode["activated_at"]
            if start is None:
                continue
            end = episode["deactivated_at"]
            if end is None:
                end = ends.get(flight_id, start)
            total += max(0.0, end - start)
        stats[flight_id] = {
            "episode_count": len(alert_map),
            "total_seconds": int(total),
            "active_count": 0,
        }
    return stats


def enrich_flight_summary(
    summary: dict[str, Any], aircraft_db: AircraftDB | None
) -> dict[str, Any]:
    enriched = enrich_from_aircraft_db(summary.get("icao", ""), aircraft_db)
    active_alerts = summary.get("active_alerts") or []
    alert_stats = summary.get("alert_stats")
    if alert_stats is None and active_alerts:
        alert_stats = live_alert_stats(active_alerts)
    result = {
        **summary,
        "callsign": summary.get("callsign") or enriched.get("callsign"),
        "model": summary.get("model") or enriched.get("model"),
        "owner": summary.get("owner") or enriched.get("owner"),
        "country": summary.get("country") or enriched.get("country"),
        "aircraft_type": summary.get("aircraft_type") or enriched.get("aircraft_type"),
    }
    if alert_stats is not None:
        result["alert_stats"] = alert_stats
    return result


def flight_summary(
    doc: dict[str, Any], last_tel: dict[str, Any] | None, aircraft_db: AircraftDB | None
) -> dict[str, Any]:
    icao = doc.get("icao", "")
    enriched = enrich_from_aircraft_db(icao, aircraft_db)
    info = doc.get("info", {})
    lat = lon = alt = speed = heading = timestamp = None
    if last_tel:
        tel = telemetry_point(last_tel)
        lat = tel.get("latitude")
        lon = tel.get("longitude")
        alt = tel.get("altitude")
        speed = tel.get("speed")
        heading = tel.get("heading")
        timestamp = tel.get("timestamp")
    is_live = doc.get("status") == FLIGHT_STATUS_LIVE
    active_alerts = format_active_alerts(doc)
    alert_stats = (
        live_alert_stats(active_alerts)
        if is_live and active_alerts
        else doc.get("alert_stats")
    )
    summary = {
        "flight_id": doc["_id"],
        "icao": icao,
        "active_alerts": active_alerts,
        "start_time": doc.get("start_time"),
        "end_time": doc.get("end_time"),
        "callsign": doc.get("callsign")
        or info.get("callsign")
        or enriched.get("callsign"),
        "model": doc.get("model") or info.get("model") or enriched.get("model"),
        "owner": doc.get("owner") or info.get("owner") or enriched.get("owner"),
        "country": doc.get("country") or info.get("country") or enriched.get("country"),
        "aircraft_type": doc.get("aircraft_type")
        or info.get("aircraft_type")
        or enriched.get("aircraft_type"),
        "latitude": lat,
        "longitude": lon,
        "altitude": alt,
        "speed": speed,
        "heading": heading,
        "is_live": is_live,
        "status": doc.get("status", "completed"),
        "retained": doc.get("retained", False),
        "timestamp": timestamp,
    }
    if alert_stats is not None:
        summary["alert_stats"] = alert_stats
    return summary


def enrich_flight_detail(
    flight_data: dict[str, Any], icao: str, aircraft_db: AircraftDB | None
) -> dict[str, Any]:
    enriched = enrich_from_aircraft_db(icao, aircraft_db)
    active_alerts = flight_data.get("active_alerts") or []
    alert_stats = flight_data.get("alert_stats")
    if alert_stats is None and active_alerts:
        alert_stats = live_alert_stats(active_alerts)
    result = {
        **flight_data,
        "callsign": flight_data.get("callsign") or enriched.get("callsign"),
        "model": flight_data.get("model") or enriched.get("model"),
        "owner": flight_data.get("owner") or enriched.get("owner"),
        "country": flight_data.get("country") or enriched.get("country"),
        "aircraft_type": flight_data.get("aircraft_type")
        or enriched.get("aircraft_type"),
        "registration": flight_data.get("registration") or enriched.get("registration"),
        "photo_url": enriched.get("photo_url"),
        "photo_photographer": enriched.get("photo_photographer"),
        "photo_link": enriched.get("photo_link"),
    }
    if alert_stats is not None:
        result["alert_stats"] = alert_stats
    return result


def app_config_payload(config: Config) -> dict[str, Any]:
    return {
        "home": {
            "latitude": config.home.latitude,
            "longitude": config.home.longitude,
        },
        "remember_planes": config.tracking.remember_planes,
    }
