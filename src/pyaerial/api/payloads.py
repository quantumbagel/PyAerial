"""Payload formatting and enrichment for the web portal."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Iterable
from urllib.parse import urlparse

from pyaerial.enrich.aircraft_db import AircraftDB, normalize_photo_url
from pyaerial.config.schema import Config
from pyaerial.jsonutil import sanitize


def sanitize_for_json(data: Any) -> Any:
    """Convert non-finite floats to null so responses are valid JSON."""
    return sanitize(data)


FLIGHT_STATUS_LIVE = "live"

#: Aircraft metadata fields shared by live and history flight payloads.
TEXT_FIELDS = ("callsign", "model", "owner", "country", "aircraft_type", "registration")
_PLACEHOLDERS = frozenset({"", "unknown", "unknown model", "unknown owner"})


def clean_text(value: Any) -> Any:
    """Map enrichment placeholders ('Unknown Model', 'UNKNOWN', '') to ``None``."""
    if isinstance(value, str):
        text = value.strip()
        return None if text.lower() in _PLACEHOLDERS else text
    return value


def first_text(*values: Any) -> Any:
    """First value that is not ``None`` or a placeholder string."""
    for value in values:
        cleaned = clean_text(value)
        if cleaned is not None:
            return cleaned
    return None


def merge_text_fields(*sources: dict[str, Any]) -> dict[str, Any]:
    """Resolve :data:`TEXT_FIELDS` across sources in priority order."""
    return {
        name: first_text(*(source.get(name) for source in sources))
        for name in TEXT_FIELDS
    }


def https_url(value: Any) -> str | None:
    """Return ``value`` only if it is an absolute https: URL."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        parsed = urlparse(text)
        host = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or not host:
        return None
    return text


def alert_id_for(doc: dict[str, Any], flight_id: Any = None) -> str:
    """Stable alert id: the stored one, else ``flight:zone:rule``. Never empty."""
    existing = doc.get("alert_id")
    if existing:
        return str(existing)
    owner = doc.get("flight_id") or flight_id or ""
    return f"{owner}:{doc.get('zone') or ''}:{doc.get('rule') or ''}"


def antenna_payload(config: Config) -> dict[str, Any]:
    """Station location and configured receivers for the Beast/config payload."""
    receivers: list[dict[str, Any]] = []
    for name, receiver in config.receivers.items():
        item: dict[str, Any] = {"name": name, "type": receiver.type}
        if receiver.host:
            item["host"] = receiver.host
        if receiver.port is not None:
            item["port"] = receiver.port
        fmt = receiver.format or receiver.options.get("format")
        if isinstance(fmt, str) and fmt:
            item["format"] = fmt.lower()
        elif receiver.type == "dump1090":
            if receiver.port == 30005:
                item["format"] = "beast"
            else:
                item["format"] = "avr"
        receivers.append(item)
    return {
        "home": {
            "latitude": config.home.latitude,
            "longitude": config.home.longitude,
        },
        "receivers": receivers,
    }


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
        "callsign": clean_text(meta.get("callsign")),
        "model": clean_text(meta.get("model")),
        "owner": clean_text(meta.get("owner")),
        "country": clean_text(meta.get("country")),
        "aircraft_type": clean_text(meta.get("typecode")),
        "registration": clean_text(meta.get("registration")),
        "photo_url": https_url(normalize_photo_url(meta.get("photo_url"))),
        "photo_photographer": clean_text(meta.get("photo_photographer")),
        "photo_link": https_url(meta.get("photo_link")),
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
    return point


def _alert_coords(doc: dict[str, Any]) -> tuple[Any, Any]:
    if doc.get("latitude") is not None or doc.get("longitude") is not None:
        return doc.get("latitude"), doc.get("longitude")
    position = doc.get("position") or {}
    coords = position.get("coordinates") or [None, None]
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None, None
    return coords[1], coords[0]


def format_alert(doc: dict[str, Any]) -> dict[str, Any]:
    latitude, longitude = _alert_coords(doc)
    return {
        "alert_id": alert_id_for(doc),
        "flight_id": doc.get("flight_id"),
        "icao": doc.get("icao"),
        "callsign": clean_text(doc.get("callsign")),
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
    flight_id = doc.get("flight_id") or doc.get("_id")
    return [
        {
            "alert_id": alert_id_for(item, flight_id),
            "zone": item.get("zone") or "",
            "rule": item.get("rule") or "",
            "activated_at": item.get("activated_at"),
            "eta": item.get("eta"),
        }
        for item in alerts
        if isinstance(item, dict)
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
    alerts: Iterable[dict[str, Any]],
    flight_ids: list[str],
    *,
    flight_ends: dict[str, float] | None = None,
) -> dict[str, dict[str, int]]:
    if not flight_ids:
        return {}
    ends = flight_ends or {}
    episodes: dict[str, dict[str, dict[str, float | None]]] = defaultdict(dict)
    for doc in alerts:
        flight_id = doc.get("flight_id")
        if not flight_id:
            continue
        alert_id = alert_id_for(doc)
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
    active_alerts = format_active_alerts(summary)
    alert_stats = summary.get("alert_stats")
    if alert_stats is None and active_alerts:
        alert_stats = live_alert_stats(active_alerts)
    result = {
        **summary,
        "active_alerts": active_alerts,
        **merge_text_fields(summary, enriched),
    }
    if alert_stats is not None:
        result["alert_stats"] = alert_stats
    return result


def flight_summary(
    doc: dict[str, Any], last_tel: dict[str, Any] | None, aircraft_db: AircraftDB | None
) -> dict[str, Any]:
    icao = doc.get("icao", "")
    enriched = enrich_from_aircraft_db(icao, aircraft_db)
    info = doc.get("info") or {}
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
        **merge_text_fields(doc, info, enriched),
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
    active_alerts = format_active_alerts(flight_data)
    alert_stats = flight_data.get("alert_stats")
    if alert_stats is None and active_alerts:
        alert_stats = live_alert_stats(active_alerts)
    result = {
        **flight_data,
        "active_alerts": active_alerts,
        **merge_text_fields(flight_data, enriched),
        "photo_url": https_url(enriched.get("photo_url")),
        "photo_photographer": enriched.get("photo_photographer"),
        "photo_link": https_url(enriched.get("photo_link")),
    }
    if alert_stats is not None:
        result["alert_stats"] = alert_stats
    return result


def app_config_payload(config: Config) -> dict[str, Any]:
    antenna = antenna_payload(config)
    payload: dict[str, Any] = {
        "home": antenna["home"],
        "remember_planes": config.tracking.remember_planes,
        "receivers": antenna["receivers"],
        "beast_websocket": "/ws/beast",
    }
    if config.web.beast_port is not None:
        payload["beast_host"] = config.web.beast_host
        payload["beast_port"] = config.web.beast_port
    return payload
