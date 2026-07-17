"""
Lightweight webapp server for live flight tracking.

Reads live flights from Redis and retained historical flights from MongoDB.
"""
from __future__ import annotations

import json
import logging
import math
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pymongo

from pyaerial.calc.aircraft_db import AircraftDB, normalize_photo_url
from pyaerial.config import load_config
from pyaerial.config.schema import Config
from pyaerial.constants import DEFAULT_AIRCRAFT_DB
from pyaerial.store.redis_live import RedisLiveStore

log = logging.getLogger("pyaerial.webapp")

_FLIGHT_STATUS_LIVE = "live"


def _connect_stores(config_path: str) -> tuple[Config, pymongo.MongoClient, pymongo.database.Database, RedisLiveStore]:
    config = load_config(config_path)
    client = pymongo.MongoClient(config.database.uri)
    if config.database.name:
        db = client.get_database(config.database.name)
    else:
        try:
            db = client.get_default_database()
        except Exception:
            db = client.get_database("pyaerial")
    live_store = RedisLiveStore(config.database.redis_uri)
    return config, client, db, live_store


def _zones_payload(config: Config) -> dict[str, Any]:
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
            rules.append({
                "name": rule.name,
                "when": when,
                "dwell_seconds": rule.dwell_seconds,
            })
        zones.append({
            "name": name,
            "coordinates": zone.coordinates,
            "rules": rules,
        })
    return {
        "home": {
            "latitude": config.home.latitude,
            "longitude": config.home.longitude,
        },
        "zones": zones,
    }


def _view_from_query(query: dict[str, list[str]]) -> str:
    values = query.get("view", ["live"])
    view = values[0].lower()
    return view if view in ("live", "history") else "live"


def _enrich_from_aircraft_db(icao: str, aircraft_db: AircraftDB | None) -> dict[str, str | None]:
    if not aircraft_db:
        return {}
    meta = aircraft_db.lookup(icao)
    if not meta:
        return {}
    return {
        "callsign": meta.get("callsign"),
        "model": meta.get("model"),
        "owner": meta.get("owner"),
        "country": meta.get("country"),
        "aircraft_type": meta.get("typecode"),
        "typecode": meta.get("typecode"),
        "registration": meta.get("registration"),
        "photo_url": normalize_photo_url(meta.get("photo_url")),
        "photo_photographer": meta.get("photo_photographer"),
        "photo_link": meta.get("photo_link"),
    }


def _telemetry_point(doc: dict[str, Any]) -> dict[str, Any]:
    point: dict[str, Any] = {
        "timestamp": doc["timestamp"],
        "altitude": doc.get("altitude"),
        "speed": doc.get("speed", doc.get("horizontal_speed")),
        "heading": doc.get("heading", doc.get("direction")),
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


def _sanitize_for_json(data: Any) -> Any:
    """Convert non-finite floats to null so responses are valid JSON."""
    if isinstance(data, float):
        return data if math.isfinite(data) else None
    if isinstance(data, dict):
        return {key: _sanitize_for_json(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_sanitize_for_json(value) for value in data]
    if isinstance(data, tuple):
        return [_sanitize_for_json(value) for value in data]
    return data


def _alert_coords(doc: dict[str, Any]) -> tuple[Any, Any]:
    position = doc.get("position") or {}
    coords = position.get("coordinates") or [None, None]
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None, None
    return coords[1], coords[0]


def _format_alert(doc: dict[str, Any]) -> dict[str, Any]:
    latitude, longitude = _alert_coords(doc)
    alert_id = doc["alert_id"] if "alert_id" in doc else str(doc["_id"])
    return {
        "alert_id": alert_id,
        "flight_id": doc.get("flight_id"),
        "icao": doc.get("icao"),
        "callsign": doc.get("callsign"),
        "zone": doc.get("zone"),
        "level": doc.get("level"),
        "timestamp": doc.get("timestamp"),
        "eta": doc.get("eta"),
        "altitude": doc.get("altitude"),
        "latitude": latitude,
        "longitude": longitude,
    }


def _enrich_flight_summary(summary: dict[str, Any], aircraft_db: AircraftDB | None) -> dict[str, Any]:
    enriched = _enrich_from_aircraft_db(summary.get("icao", ""), aircraft_db)
    return {
        **summary,
        "callsign": summary.get("callsign") or enriched.get("callsign"),
        "model": summary.get("model") or enriched.get("model"),
        "owner": summary.get("owner") or enriched.get("owner"),
        "country": summary.get("country") or enriched.get("country"),
        "aircraft_type": summary.get("aircraft_type") or enriched.get("aircraft_type") or enriched.get("typecode"),
    }


def _flight_summary(doc: dict[str, Any], last_tel: dict[str, Any] | None,
                    aircraft_db: AircraftDB | None) -> dict[str, Any]:
    icao = doc.get("icao", "")
    enriched = _enrich_from_aircraft_db(icao, aircraft_db)
    info = doc.get("info", {})
    lat = lon = alt = speed = heading = None
    if last_tel:
        tel = _telemetry_point(last_tel)
        lat = tel.get("latitude")
        lon = tel.get("longitude")
        alt = tel.get("altitude")
        speed = tel.get("speed")
        heading = tel.get("heading")
    is_live = doc.get("status") == _FLIGHT_STATUS_LIVE
    return {
        "flight_id": doc["_id"],
        "icao": icao,
        "zone": doc.get("zone"),
        "level": doc.get("level"),
        "start_time": doc.get("start_time"),
        "end_time": doc.get("end_time"),
        "callsign": doc.get("callsign") or info.get("callsign") or enriched.get("callsign"),
        "model": doc.get("model") or info.get("model") or enriched.get("model"),
        "owner": doc.get("owner") or info.get("owner") or enriched.get("owner"),
        "country": doc.get("country") or info.get("country") or enriched.get("country"),
        "aircraft_type": doc.get("aircraft_type") or info.get("aircraft_type") or info.get("typecode") or enriched.get("aircraft_type") or enriched.get("typecode"),
        "latitude": lat,
        "longitude": lon,
        "altitude": alt,
        "speed": speed,
        "heading": heading,
        "is_live": is_live,
        "status": doc.get("status", "completed"),
        "retained": doc.get("retained", False),
    }


class WebAppHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        log.debug(format, *args)

    @property
    def db(self) -> pymongo.database.Database:
        return self.server.db

    @property
    def live_store(self) -> RedisLiveStore:
        return self.server.live_store

    @property
    def aircraft_db(self) -> AircraftDB | None:
        return self.server.aircraft_db

    @property
    def config(self) -> Config:
        return self.server.config

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
        elif path == "/api/flights":
            self.handle_api_flights(query)
        elif path == "/api/flight":
            self.handle_api_flight(query)
        elif path == "/api/telemetry":
            self.handle_api_telemetry(query)
        elif path == "/api/live":
            self.handle_api_live(query)
        elif path == "/api/alerts":
            self.handle_api_alerts(query)
        elif path == "/api/zones":
            self.handle_api_zones()
        else:
            self.send_error(404, "Not Found")

    def handle_api_flights(self, query: dict[str, list[str]] | None = None):
        view = _view_from_query(query or {})
        try:
            if view == "live":
                results = [
                    _enrich_flight_summary(summary, self.aircraft_db)
                    for summary in self.live_store.get_flights()
                ]
                self.send_json(results)
                return

            flights_col = self.db.get_collection("flights")
            telemetry_col = self.db.get_collection("telemetry")
            alerts_col = self.db.get_collection("alerts")

            completed_cursor = flights_col.find({
                "status": {"$ne": _FLIGHT_STATUS_LIVE},
                "$or": [{"retained": True}, {"retained": {"$exists": False}}],
            }).sort("end_time", -1).limit(100)

            completed_docs = []
            for doc in completed_cursor:
                if doc.get("retained") or alerts_col.count_documents({"flight_id": doc["_id"]}, limit=1):
                    completed_docs.append(doc)
                if len(completed_docs) >= 50:
                    break

            results = []
            for doc in completed_docs:
                last_tel = telemetry_col.find_one(
                    {"flight_id": doc["_id"]},
                    sort=[("timestamp", pymongo.DESCENDING)],
                )
                results.append(_flight_summary(doc, last_tel, self.aircraft_db))

            self.send_json(results)
        except Exception as exc:
            self.send_error(500, f"Database error: {exc}")

    def handle_api_flight(self, query: dict[str, list[str]]):
        flight_ids = query.get("flight_id", [])
        if not flight_ids:
            self.send_error(400, "Missing flight_id parameter")
            return
        flight_id = flight_ids[0]
        view = _view_from_query(query)
        try:
            if view == "live":
                flight_data = self.live_store.get_flight(flight_id)
                if not flight_data:
                    self.send_error(404, "Flight not found")
                    return
                icao = flight_data.get("icao", "")
                enriched = _enrich_from_aircraft_db(icao, self.aircraft_db)
                flight_data = {
                    **flight_data,
                    "callsign": flight_data.get("callsign") or enriched.get("callsign"),
                    "model": flight_data.get("model") or enriched.get("model"),
                    "owner": flight_data.get("owner") or enriched.get("owner"),
                    "country": flight_data.get("country") or enriched.get("country"),
                    "aircraft_type": flight_data.get("aircraft_type") or enriched.get("typecode"),
                    "registration": flight_data.get("registration") or enriched.get("registration"),
                    "photo_url": enriched.get("photo_url"),
                    "photo_photographer": enriched.get("photo_photographer"),
                    "photo_link": enriched.get("photo_link"),
                }
                self.send_json(flight_data)
                return

            doc = self.db.get_collection("flights").find_one({"_id": flight_id})
            if not doc:
                self.send_error(404, "Flight not found")
                return
            icao = doc.get("icao", "")
            enriched = _enrich_from_aircraft_db(icao, self.aircraft_db)
            info = doc.get("info", {})
            flight_data = {
                "flight_id": doc["_id"],
                "icao": icao,
                "zone": doc.get("zone"),
                "level": doc.get("level"),
                "start_time": doc.get("start_time"),
                "end_time": doc.get("end_time"),
                "callsign": doc.get("callsign") or info.get("callsign") or enriched.get("callsign"),
                "model": doc.get("model") or info.get("model") or enriched.get("model"),
                "owner": doc.get("owner") or info.get("owner") or enriched.get("owner"),
                "country": doc.get("country") or info.get("country") or enriched.get("country"),
                "aircraft_type": doc.get("aircraft_type") or info.get("aircraft_type") or info.get("typecode") or enriched.get("aircraft_type") or enriched.get("typecode"),
                "registration": doc.get("registration") or info.get("registration") or enriched.get("registration"),
                "photo_url": enriched.get("photo_url"),
                "photo_photographer": enriched.get("photo_photographer"),
                "photo_link": enriched.get("photo_link"),
                "raw_messages": doc.get("raw_messages", []),
                "is_live": False,
                "status": doc.get("status", "completed"),
            }
            self.send_json(flight_data)
        except Exception as exc:
            self.send_error(500, f"Database error: {exc}")

    def handle_api_telemetry(self, query: dict[str, list[str]]):
        flight_ids = query.get("flight_id", [])
        if not flight_ids:
            self.send_error(400, "Missing flight_id parameter")
            return
        flight_id = flight_ids[0]
        view = _view_from_query(query)
        try:
            if view == "live":
                points = self.live_store.get_telemetry(flight_id)
                self.send_json(points)
                return

            cursor = self.db.get_collection("telemetry").find({"flight_id": flight_id}).sort("timestamp", 1)
            points = [_telemetry_point(doc) for doc in cursor]
            self.send_json(points)
        except Exception as exc:
            self.send_error(500, f"Database error: {exc}")

    def handle_api_live(self, query: dict[str, list[str]]):
        since_vals = query.get("since", [])
        since = float(since_vals[0]) if since_vals else 0.0
        now = time.time()
        try:
            points = self.live_store.get_live_telemetry(since)
            self.send_json({"telemetry": points, "timestamp": now})
        except Exception as exc:
            self.send_error(500, f"Database error: {exc}")

    def handle_api_alerts(self, query: dict[str, list[str]]):
        since_vals = query.get("since", [])
        flight_vals = query.get("flight_id", [])
        level_vals = query.get("level", [])
        since = float(since_vals[0]) if since_vals else 0.0
        flight_id = flight_vals[0] if flight_vals else None
        level = level_vals[0] if level_vals else None
        view = _view_from_query(query)

        try:
            if view == "live":
                alerts = self.live_store.get_alerts(since=since, flight_id=flight_id, level=level)
                self.send_json([_format_alert(alert) for alert in alerts])
                return

            filt: dict[str, Any] = {}
            if since:
                filt["timestamp"] = {"$gt": since}
            if flight_id:
                filt["flight_id"] = flight_id
            if level:
                filt["level"] = level

            cursor = self.db.get_collection("alerts").find(filt).sort("timestamp", -1).limit(100)
            self.send_json([_format_alert(doc) for doc in cursor])
        except Exception as exc:
            self.send_error(500, f"Database error: {exc}")

    def handle_api_zones(self):
        try:
            self.send_json(_zones_payload(self.config))
        except Exception as exc:
            self.send_error(500, f"Configuration error: {exc}")

    def send_json(self, data: Any):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(_sanitize_for_json(data)).encode("utf-8"))


def run_webapp(config_path: str = "config.yaml", *,
               aircraft_db_path: str = DEFAULT_AIRCRAFT_DB,
               host: str = "0.0.0.0", port: int = 10090) -> None:
    config, client, db, live_store = _connect_stores(config_path)
    aircraft_db = AircraftDB(aircraft_db_path) if aircraft_db_path else None

    server = ThreadingHTTPServer((host, port), WebAppHandler)
    server.config = config
    server.db = db
    server.live_store = live_store
    server.aircraft_db = aircraft_db

    actual_host, actual_port = server.server_address
    print(f"Starting PyAerial web portal on http://localhost:{actual_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web server...")
    finally:
        server.server_close()
        client.close()
        live_store.close()
        if aircraft_db:
            aircraft_db.close()


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PyAerial Live Flight Tracker</title>
    <!-- Leaflet CSS -->
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <!-- Google Fonts Outfit & JetBrains Mono -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }
        body {
            font-family: 'Outfit', system-ui, -apple-system, sans-serif;
            background-color: #121212;
            color: #e0e0e0;
            display: flex;
            height: 100vh;
            overflow: hidden;
        }
        #sidebar {
            width: 360px;
            background-color: #1a1a1a;
            border-right: 1px solid #2d2d2d;
            display: flex;
            flex-direction: column;
            height: 100%;
            z-index: 10;
        }
        #sidebar-header {
            padding: 20px;
            border-bottom: 1px solid #2d2d2d;
            background-color: #151515;
        }
        #sidebar-header h1 {
            font-size: 1.2rem;
            font-weight: 600;
            color: #3b82f6;
            margin-bottom: 3px;
        }
        #sidebar-header p {
            font-size: 0.75rem;
            color: #888;
        }
        #search-container {
            padding: 12px 20px;
            border-bottom: 1px solid #2d2d2d;
            background-color: #151515;
        }
        #search-input {
            width: 100%;
            padding: 8px 12px;
            border-radius: 4px;
            border: 1px solid #2d2d2d;
            background-color: #222;
            color: #fff;
            font-family: inherit;
            font-size: 0.8rem;
            outline: none;
        }
        #search-input:focus {
            border-color: #3b82f6;
        }
        #filter-container {
            padding: 0 20px 12px 20px;
            background-color: #151515;
            border-bottom: 1px solid #2d2d2d;
            display: flex;
        }
        #warning-filter {
            width: 100%;
            padding: 8px 12px;
            border-radius: 4px;
            border: 1px solid #2d2d2d;
            background-color: #222;
            color: #fff;
            font-family: inherit;
            font-size: 0.8rem;
            outline: none;
            cursor: pointer;
        }
        #warning-filter:focus {
            border-color: #3b82f6;
        }
        #stats-panel {
            padding: 12px 20px;
            background-color: #151515;
            border-bottom: 1px solid #2d2d2d;
            font-size: 0.8rem;
            display: flex;
            gap: 8px;
        }
        .stat-card {
            flex: 1;
            background-color: #222;
            padding: 6px 10px;
            border-radius: 6px;
            border: 1px solid #2d2d2d;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            min-width: 0;
        }
        #flight-list {
            flex-grow: 1;
            overflow-y: auto;
            list-style: none;
        }
        .flight-item {
            padding: 12px 20px;
            border-bottom: 1px solid #262626;
            cursor: pointer;
            background-color: transparent;
            transition: background-color 0.15s;
        }
        .flight-item:hover {
            background-color: #222;
        }
        .flight-item.active {
            background-color: #1a2744;
            border-left: 4px solid #3b82f6;
            box-shadow: inset 4px 0 14px rgba(59, 130, 246, 0.12);
        }
        .flight-meta-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .flight-callsign {
            font-weight: 600;
            font-size: 0.9rem;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background-color: #64748b;
        }
        .status-dot.live {
            background-color: #10b981;
            box-shadow: 0 0 6px #10b981;
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
            70% { transform: scale(1); box-shadow: 0 0 0 4px rgba(16, 185, 129, 0); }
            100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }
        .flight-icao {
            font-family: 'JetBrains Mono', monospace;
            background-color: #2d2d2d;
            padding: 1px 4px;
            border-radius: 3px;
            font-size: 0.7rem;
            color: #ccc;
            font-weight: 500;
        }
        .flight-desc {
            font-size: 0.75rem;
            color: #888;
        }
        .flight-time {
            font-size: 0.7rem;
            color: #555;
            text-align: right;
        }
        #map-container {
            flex-grow: 1;
            position: relative;
            height: 100%;
        }
        #map {
            width: 100%;
            height: 100%;
            background-color: #1a1a1a;
        }
        #map-controls {
            position: absolute;
            top: 12px;
            left: 12px;
            z-index: 1005;
            display: flex;
            align-items: stretch;
            gap: 0;
            background: rgba(15, 18, 24, 0.9);
            border: 1px solid rgba(51, 65, 85, 0.8);
            border-radius: 8px;
            padding: 4px;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.45);
            backdrop-filter: blur(8px);
        }
        .map-toolbar-group {
            display: flex;
            gap: 2px;
        }
        .map-toolbar-divider {
            width: 1px;
            background: rgba(51, 65, 85, 0.9);
            margin: 4px 4px;
            align-self: stretch;
        }
        #follow-btn,
        #zones-btn,
        #paths-btn,
        .map-zoom-btn {
            padding: 8px 12px;
            border-radius: 5px;
            border: 1px solid transparent;
            background-color: transparent;
            color: #94a3b8;
            font-family: inherit;
            font-size: 0.8rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.15s;
            line-height: 1;
        }
        .map-zoom-btn {
            padding: 8px 14px;
            font-size: 1.1rem;
            font-weight: 500;
        }
        #follow-btn {
            display: none;
        }
        #follow-btn:hover,
        #zones-btn:hover,
        #paths-btn:hover,
        .map-zoom-btn:hover {
            background-color: rgba(34, 34, 34, 0.9);
            color: #fff;
        }
        #follow-btn.active,
        #zones-btn.active,
        #paths-btn.active {
            background-color: #1e3a5f;
            border-color: #3b82f6;
            color: #dbeafe;
        }
        .zone-label {
            background: rgba(15, 18, 24, 0.88);
            border: 1px solid rgba(245, 158, 11, 0.45);
            color: #fcd34d;
            font-family: 'Outfit', system-ui, sans-serif;
            font-size: 0.72rem;
            font-weight: 600;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            padding: 2px 8px;
            border-radius: 4px;
            white-space: nowrap;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.35);
        }
        .zone-popup {
            font-family: 'Outfit', system-ui, sans-serif;
            font-size: 0.8rem;
            color: #e2e8f0;
        }
        .zone-popup h4 {
            margin: 0 0 8px 0;
            color: #fcd34d;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            font-size: 0.85rem;
        }
        .zone-popup .rule {
            margin-top: 6px;
            padding-top: 6px;
            border-top: 1px solid #334155;
            color: #cbd5e1;
        }
        .zone-popup .rule-name {
            font-weight: 600;
            text-transform: uppercase;
            font-size: 0.72rem;
            letter-spacing: 0.04em;
        }
        .zone-popup .rule-name.warn { color: #fcd34d; }
        .zone-popup .rule-name.alert { color: #fca5a5; }
        .leaflet-popup-content-wrapper,
        .leaflet-popup-tip {
            background: #1a1a1a;
            color: #e2e8f0;
            border: 1px solid #334155;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.45);
        }
        .leaflet-popup-content {
            margin: 12px 14px;
        }
        /* Details Drawer Styling */
        #details-drawer {
            position: absolute;
            top: 0;
            right: -420px;
            width: 420px;
            height: 100%;
            background-color: #1a1a1a;
            border-left: 1px solid #2d2d2d;
            z-index: 1010;
            transition: right 0.3s ease;
            display: flex;
            flex-direction: column;
        }
        #details-drawer.open {
            right: 0;
        }
        #drawer-header {
            padding: 20px;
            border-bottom: 1px solid #2d2d2d;
            background-color: #151515;
            position: relative;
        }
        #drawer-header h2 {
            font-size: 1.1rem;
            font-weight: 600;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 8px;
            margin-top: 6px;
        }
        .close-btn {
            background: none;
            border: none;
            color: #888;
            font-size: 1.5rem;
            cursor: pointer;
            position: absolute;
            top: 12px;
            right: 16px;
            z-index: 30;
            line-height: 1;
            transition: color 0.15s;
        }
        .close-btn:hover {
            color: #ef4444;
        }
        .flight-path {
            cursor: pointer;
        }
        .drawer-content {
            flex-grow: 1;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
        }
        .info-section {
            padding: 16px 20px;
            border-bottom: 1px solid #2d2d2d;
        }
        .info-section h3 {
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: #3b82f6;
            margin-bottom: 10px;
            font-weight: 600;
        }
        .details-grid {
            display: grid;
            grid-template-columns: 130px 1fr;
            gap: 6px 20px;
            font-size: 0.75rem;
            max-width: 340px;
        }
        .details-label {
            color: #888;
        }
        .details-value {
            color: #fff;
            text-align: left;
            font-weight: 500;
        }
        .drawer-tabs {
            display: flex;
            border-bottom: 1px solid #2d2d2d;
            background-color: #151515;
        }
        .tab-btn {
            flex: 1;
            padding: 12px;
            background: none;
            border: none;
            border-bottom: 2px solid transparent;
            color: #888;
            font-size: 0.8rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            font-family: inherit;
        }
        .tab-btn:hover {
            color: #fff;
            background-color: #222;
        }
        .tab-btn.active {
            color: #3b82f6;
            border-bottom-color: #3b82f6;
            background-color: #1a1a1a;
        }
        .tab-content {
            padding: 16px;
            flex-grow: 1;
            overflow-y: auto;
            background-color: #121212;
            display: flex;
            flex-direction: column;
        }
        .terminal-list {
            background-color: #121212;
            border: 1px solid #2d2d2d;
            border-radius: 4px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.75rem;
            padding: 6px;
            flex-grow: 1;
            min-height: 220px;
            max-height: 340px;
            overflow-y: auto;
            color: #38bdf8;
        }
        .terminal-line {
            display: flex;
            padding: 5px 8px;
            margin-bottom: 1px;
            line-height: 1.5;
            border-radius: 3px;
        }
        .terminal-line:nth-child(odd) {
            background-color: rgba(255, 255, 255, 0.03);
        }
        .terminal-line:nth-child(even) {
            background-color: rgba(0, 0, 0, 0.15);
        }
        .terminal-time {
            color: #555;
            margin-right: 10px;
            user-select: none;
        }
        .terminal-hex {
            color: #34d399;
            font-weight: 500;
        }
        .table-container {
            border: 1px solid #2d2d2d;
            border-radius: 4px;
            overflow: hidden;
            background-color: #121212;
            max-height: 340px;
            overflow-y: auto;
        }
        .tel-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.75rem;
            text-align: left;
        }
        .tel-table th, .tel-table td {
            padding: 8px 10px;
            border-bottom: 1px solid #2d2d2d;
        }
        .tel-table th {
            background-color: #151515;
            color: #888;
            font-weight: 600;
        }
        .tel-table td {
            color: #ccc;
        }
        .plane-icon-div {
            background: none;
            border: none;
        }
        /* Custom scrollbar */
        ::-webkit-scrollbar {
            width: 5px;
        }
        ::-webkit-scrollbar-track {
            background: #1a1a1a;
        }
        ::-webkit-scrollbar-thumb {
            background: #333;
            border-radius: 3px;
        }
        .level-badge {
            font-size: 0.65rem;
            font-weight: 600;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            padding: 2px 6px;
            border-radius: 4px;
            background: #334155;
            color: #cbd5e1;
        }
        .level-badge.live { background: #064e3b; color: #6ee7b7; }
        .level-badge.warn { background: #78350f; color: #fcd34d; }
        .level-badge.alert { background: #7f1d1d; color: #fca5a5; }
        .level-badge.done { background: #1e293b; color: #94a3b8; }
        #sidebar-tabs {
            display: flex;
            border-bottom: 1px solid #2d2d2d;
            background: #151515;
        }
        .sidebar-tab {
            flex: 1;
            padding: 10px 8px;
            border: none;
            background: transparent;
            color: #888;
            font-family: inherit;
            font-size: 0.8rem;
            cursor: pointer;
        }
        .sidebar-tab.active {
            color: #3b82f6;
            border-bottom: 2px solid #3b82f6;
        }
        .sidebar-panel { display: none; flex-direction: column; flex-grow: 1; overflow: hidden; }
        .sidebar-panel.active { display: flex; }
        .alert-item {
            padding: 12px 20px;
            border-bottom: 1px solid #262626;
            cursor: pointer;
        }
        .alert-item:hover { background: #222; }
        .alert-item.active { background: #1e2638; border-left: 3px solid #f59e0b; }
        .alert-meta { display: flex; justify-content: space-between; align-items: center; }
        .alert-level-warn { color: #fcd34d; }
        .alert-level-alert { color: #fca5a5; }
        #alert-timeline-list { display: flex; flex-direction: column; gap: 8px; }
        .alert-timeline-item {
            padding: 10px 12px;
            border-left: 3px solid #334155;
            background: #0f1218;
            border-radius: 0 6px 6px 0;
            font-size: 0.85rem;
        }
        .alert-timeline-item.warn { border-left-color: #f59e0b; }
        .alert-timeline-item.alert { border-left-color: #ef4444; }
        #view-toggle {
            display: flex;
            gap: 8px;
            padding: 12px 20px;
            border-bottom: 1px solid #2d2d2d;
            background-color: #151515;
        }
        .view-btn {
            flex: 1;
            padding: 8px 10px;
            border-radius: 6px;
            border: 1px solid #2d2d2d;
            background: #222;
            color: #94a3b8;
            font-family: inherit;
            font-size: 0.8rem;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.15s;
        }
        .view-btn.active {
            background: #1e3a5f;
            border-color: #3b82f6;
            color: #dbeafe;
        }
        #detail-photo-container {
            display: none;
            padding: 0;
            border-bottom: 1px solid #2d2d2d;
            position: relative;
            background: #0c0f16;
            overflow: hidden;
            height: 180px;
        }
        .photo-gradient-top {
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 80px;
            background: linear-gradient(to bottom, rgba(0, 0, 0, 0.75) 0%, rgba(0, 0, 0, 0.35) 60%, transparent 100%);
            z-index: 4;
            pointer-events: none;
        }
        .photo-title-overlay {
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            padding: 14px 16px 10px;
            z-index: 5;
        }
        .photo-title-overlay h2 {
            font-size: 1.1rem;
            font-weight: 600;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 8px;
            margin-top: 4px;
            text-shadow: 0 1px 4px rgba(0, 0, 0, 0.6);
        }
        .photo-gradient-bottom {
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            padding: 10px 16px;
            background: linear-gradient(transparent, rgba(0, 0, 0, 0.9));
            font-size: 0.65rem;
            color: #94a3b8;
            display: flex;
            justify-content: space-between;
            align-items: center;
            z-index: 5;
        }
        #drawer-header.no-photo h2 {
            margin-top: 6px;
        }
        #drawer-header.has-photo {
            padding: 12px 20px;
        }
        #drawer-header.has-photo h2,
        #drawer-header.has-photo .drawer-header-label {
            display: none;
        }
        #details-drawer.has-photo-drawer .close-btn {
            color: #fff;
            text-shadow: 0 1px 4px rgba(0, 0, 0, 0.85);
        }
        #details-drawer.has-photo-drawer .close-btn:hover {
            color: #fca5a5;
        }
    </style>
</head>
<body>
    <div id="sidebar">
        <div id="sidebar-header">
            <h1>PyAerial Live Tracker</h1>
            <p>See the data captured by your ADS-B receiver</p>
        </div>
        <div id="view-toggle">
            <button class="view-btn active" id="view-live" onclick="switchPortalView('live')">Live</button>
            <button class="view-btn" id="view-history" onclick="switchPortalView('history')">Historical</button>
        </div>
        <div id="search-container">
            <input type="text" id="search-input" placeholder="Search by callsign, ICAO, or model..." />
        </div>
        <div id="filter-container" style="padding: 0 24px 12px 24px; background-color: #13171f; display: flex; gap: 8px;">
            <select id="warning-filter" style="width: 100%; padding: 10px 14px; border-radius: 8px; border: 1px solid #334155; background-color: #0f1218; color: #f1f5f9; font-family: inherit; font-size: 0.85rem; cursor: pointer; transition: all 0.2s;">
                <option value="all">All Flights</option>
                <option value="warn">Warnings (Warn)</option>
                <option value="alert">Alerts (Alert)</option>
                <option value="any">Any Warning/Alert</option>
            </select>
        </div>
        <div id="stats-panel">
            <div class="stat-card">
                <span id="flight-stat-label">Live:</span>
                <strong id="flight-count" style="color: #10b981;">0</strong>
            </div>
            <div class="stat-card" id="live-stat-card">
                <span>Tracking:</span>
                <strong id="live-count" style="color: #6366f1;">0</strong>
            </div>
            <div class="stat-card">
                <span>Alerts:</span>
                <strong id="alert-count" style="color: #f59e0b;">0</strong>
            </div>
        </div>
        <div id="sidebar-tabs">
            <button class="sidebar-tab active" id="tab-flights" onclick="switchSidebarTab('flights')">Flights</button>
            <button class="sidebar-tab" id="tab-alerts" onclick="switchSidebarTab('alerts')">Alerts</button>
        </div>
        <div id="panel-flights" class="sidebar-panel active">
            <ul id="flight-list"></ul>
        </div>
        <div id="panel-alerts" class="sidebar-panel">
            <ul id="alert-list" style="list-style:none; overflow-y:auto; flex-grow:1;"></ul>
        </div>
    </div>
    <div id="map-container">
        <div id="map-controls">
            <div class="map-toolbar-group">
                <button id="follow-btn" type="button" title="Follow selected aircraft">Follow</button>
                <button id="zones-btn" type="button" title="Show configured geofence zones">Zones</button>
                <button id="paths-btn" type="button" title="Show flight paths for all visible aircraft">Paths</button>
            </div>
            <div class="map-toolbar-divider"></div>
            <div class="map-toolbar-group">
                <button id="zoom-in-btn" class="map-zoom-btn" type="button" title="Zoom in">+</button>
                <button id="zoom-out-btn" class="map-zoom-btn" type="button" title="Zoom out">−</button>
            </div>
        </div>
        <div id="map"></div>
        
        <!-- Sliding Details Drawer -->
        <div id="details-drawer">
            <button id="close-drawer-btn" class="close-btn" title="Close">&times;</button>
            <div id="drawer-header" class="no-photo">
                <div class="flight-desc drawer-header-label" style="text-transform: uppercase; letter-spacing: 0.1em; color: #94a3b8; font-weight: 600; font-size: 0.75rem;">Selected Aircraft</div>
                <h2><span id="detail-callsign">N/A</span> <span id="detail-icao" class="flight-icao">N/A</span></h2>
            </div>
            <div class="drawer-content">
                <!-- Aircraft Photo Card -->
                <div id="detail-photo-container">
                    <img id="detail-photo" src="" style="width: 100%; height: 100%; object-fit: cover; opacity: 0.85;" alt="Aircraft Photo">
                    <div class="photo-gradient-top"></div>
                    <div class="photo-title-overlay">
                        <div class="flight-desc" style="text-transform: uppercase; letter-spacing: 0.1em; color: #cbd5e1; font-weight: 600; font-size: 0.75rem;">Selected Aircraft</div>
                        <h2><span id="detail-callsign-photo">N/A</span> <span id="detail-icao-photo" class="flight-icao">N/A</span></h2>
                    </div>
                    <div class="photo-gradient-bottom">
                        <span>Photo by <span id="detail-photo-photographer" style="color: #fff; font-weight: 500;">Unknown</span></span>
                        <a id="detail-photo-link" href="#" target="_blank" style="color: #3b82f6; text-decoration: none; font-weight: 600;">View on Planespotters.net</a>
                    </div>
                </div>

                <div class="info-section">
                    <h3>Aircraft Details</h3>
                    <div class="details-grid">
                        <span class="details-label">Registration</span>
                        <span class="details-value" id="detail-registration" style="font-weight: 600; color: #3b82f6;">N/A</span>
                        <span class="details-label">Model</span>
                        <span class="details-value" id="detail-model">N/A</span>
                        <span class="details-label">Aircraft Type</span>
                        <span class="details-value" id="detail-type">N/A</span>
                        <span class="details-label">Owner</span>
                        <span class="details-value" id="detail-owner">N/A</span>
                        <span class="details-label">Registration Country</span>
                        <span class="details-value" id="detail-country">N/A</span>
                        <span class="details-label">Zone / Level</span>
                        <span class="details-value" id="detail-zone-level" style="color: #f59e0b;">N/A</span>
                    </div>
                </div>
                <div class="info-section" style="background-color: #141720;">
                    <h3>Telemetry Readings</h3>
                    <div class="details-grid">
                        <span class="details-label">Altitude</span>
                        <span class="details-value" id="detail-altitude">N/A</span>
                        <span class="details-label">Speed</span>
                        <span class="details-value" id="detail-speed">N/A</span>
                        <span class="details-label">Heading</span>
                        <span class="details-value" id="detail-heading">N/A</span>
                    </div>
                </div>
                
                <div class="drawer-tabs">
                    <button class="tab-btn active" id="tab-btn-alerts" onclick="switchTab('alerts')">Alerts</button>
                    <button class="tab-btn" id="tab-btn-telemetry" onclick="switchTab('telemetry')">Telemetry</button>
                    <button class="tab-btn" id="tab-btn-raw" onclick="switchTab('raw')">Raw</button>
                </div>
                
                <div class="tab-content" id="tab-alerts">
                    <div id="alert-timeline-list"></div>
                </div>

                <div class="tab-content" id="tab-telemetry" style="display: none;">
                    <div class="table-container">
                        <table class="tel-table">
                            <thead>
                                <tr>
                                    <th>Time</th>
                                    <th>Altitude</th>
                                    <th>Speed</th>
                                    <th>Heading</th>
                                </tr>
                            </thead>
                            <tbody id="telemetry-table-body">
                            </tbody>
                        </table>
                    </div>
                </div>
                
                <div class="tab-content" id="tab-raw" style="display: none;">
                    <div id="raw-messages-list" class="terminal-list">
                        <!-- Dynamic terminal hex rows -->
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Leaflet JS -->
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script>
        const map = L.map('map', { zoomControl: false }).setView([36.681, -78.875], 8);

        L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
            attribution: '&copy; OpenStreetMap &copy; CARTO'
        }).addTo(map);

        map.on('dragstart', () => {
            if (followSelectedPlane) {
                followSelectedPlane = false;
                updateFollowButton();
            }
        });

        function updateFollowButton() {
            const btn = document.getElementById('follow-btn');
            if (!activeFlightId) {
                btn.style.display = 'none';
                return;
            }
            btn.style.display = 'block';
            btn.classList.toggle('active', followSelectedPlane);
            btn.innerText = followSelectedPlane ? 'Following' : 'Follow';
        }

        function followPlaneOnMap(lat, lon, { initial = false } = {}) {
            if (lat == null || lon == null) return;
            if (initial) {
                map.setView([lat, lon], Math.max(map.getZoom(), 11));
            } else {
                map.panTo([lat, lon], { animate: true, duration: 0.5 });
            }
        }

        function enableFollowPlane() {
            followSelectedPlane = true;
            updateFollowButton();
            const flight = flightsData.find(f => f.flight_id === activeFlightId);
            if (flight && flight.latitude != null && flight.longitude != null) {
                followPlaneOnMap(flight.latitude, flight.longitude, { initial: true });
            }
        }

        function formatConstraint(when) {
            return Object.entries(when).map(([field, bounds]) => {
                const parts = [];
                if (bounds.min != null) parts.push(`min ${bounds.min}`);
                if (bounds.max != null) parts.push(`max ${bounds.max}`);
                return `${field}: ${parts.join(', ')}`;
            }).join(' · ');
        }

        function buildZonePopup(zone) {
            const rules = (zone.rules || []).map(rule => {
                const level = (rule.name || '').toLowerCase();
                const levelClass = level === 'alert' ? 'alert' : (level === 'warn' ? 'warn' : '');
                return `
                    <div class="rule">
                        <div class="rule-name ${levelClass}">${rule.name}</div>
                        <div>${formatConstraint(rule.when)}</div>
                        <div style="color:#94a3b8; margin-top:2px;">Dwell ${rule.dwell_seconds}s</div>
                    </div>
                `;
            }).join('');
            return `<div class="zone-popup"><h4>${zone.name}</h4>${rules || '<div>No rules configured.</div>'}</div>`;
        }

        function clearZoneLayers() {
            zoneLayers.forEach(layer => map.removeLayer(layer));
            zoneLayers = [];
            if (homeMarker) {
                map.removeLayer(homeMarker);
                homeMarker = null;
            }
        }

        function renderZones(zonesData) {
            clearZoneLayers();
            if (!zonesVisible) return;

            const home = zonesData.home;
            if (home && home.latitude != null && home.longitude != null) {
                homeMarker = L.circleMarker([home.latitude, home.longitude], {
                    radius: 6,
                    color: '#38bdf8',
                    fillColor: '#38bdf8',
                    fillOpacity: 0.95,
                    weight: 2,
                }).addTo(map);
                homeMarker.bindPopup('<div class="zone-popup"><h4>Home</h4><div>Receiver / reference location</div></div>');
                zoneLayers.push(homeMarker);
            }

            (zonesData.zones || []).forEach((zone, index) => {
                const colors = ZONE_COLORS[index % ZONE_COLORS.length];
                const polygon = L.polygon(zone.coordinates, {
                    color: colors.stroke,
                    fillColor: colors.fill,
                    fillOpacity: 0.14,
                    weight: 2,
                    opacity: 0.9,
                }).addTo(map);
                polygon.bindPopup(buildZonePopup(zone));

                const center = polygon.getBounds().getCenter();
                const label = L.marker(center, {
                    interactive: false,
                    icon: L.divIcon({
                        className: 'zone-label-marker',
                        html: `<div class="zone-label">${zone.name}</div>`,
                        iconSize: [0, 0],
                    }),
                }).addTo(map);

                zoneLayers.push(polygon, label);
            });
        }

        function updateZonesButton() {
            const btn = document.getElementById('zones-btn');
            btn.classList.toggle('active', zonesVisible);
            btn.innerText = zonesVisible ? 'Zones On' : 'Zones Off';
        }

        function updatePathsButton() {
            const btn = document.getElementById('paths-btn');
            btn.classList.toggle('active', showAllPaths);
            btn.innerText = showAllPaths ? 'Paths On' : 'Paths Off';
        }

        async function fetchZones() {
            try {
                const response = await fetch('/api/zones');
                if (!response.ok) return;
                const data = await response.json();
                renderZones(data);
            } catch (err) {
                console.error('Failed to fetch zones', err);
            }
        }

        let planeMarkers = {}; 
        let planePaths = {};
        let planeEventMarkers = {};   
        let flightsData = [];
        let alertsData = [];
        let activeFlightId = null;
        let activeAlertId = null;
        let lastSeenTimestamp = 0;
        let searchQuery = '';
        let sidebarView = 'flights';
        let portalView = 'live';
        let livePollTimer = null;
        let flightsPollTimer = null;
        let alertsPollTimer = null;
        let followSelectedPlane = false;
        let zonesVisible = true;
        let showAllPaths = false;
        let zoneLayers = [];
        let homeMarker = null;
        const ZONE_COLORS = [
            { stroke: '#f59e0b', fill: '#f59e0b' },
            { stroke: '#3b82f6', fill: '#3b82f6' },
            { stroke: '#a855f7', fill: '#a855f7' },
            { stroke: '#14b8a6', fill: '#14b8a6' },
        ];

        function viewQuery() {
            return `view=${portalView}`;
        }

        function switchPortalView(view) {
            if (portalView === view) return;
            portalView = view;
            document.getElementById('view-live').classList.toggle('active', view === 'live');
            document.getElementById('view-history').classList.toggle('active', view === 'history');
            document.getElementById('flight-stat-label').innerText = view === 'live' ? 'Live:' : 'Retained:';
            document.getElementById('live-stat-card').style.display = view === 'live' ? 'flex' : 'none';

            activeFlightId = null;
            activeAlertId = null;
            followSelectedPlane = false;
            lastSeenTimestamp = 0;
            flightsData = [];
            alertsData = [];
            Object.values(planeMarkers).forEach(marker => map.removeLayer(marker));
            Object.values(planePaths).forEach(path => map.removeLayer(path));
            Object.values(planeEventMarkers).forEach(markers => markers.forEach(marker => map.removeLayer(marker)));
            planeMarkers = {};
            planePaths = {};
            planeEventMarkers = {};
            document.getElementById('details-drawer').classList.remove('open');
            updateFollowButton();
            renderFlightList();
            renderAlertList();
            restartPolling();
        }

        function restartPolling() {
            if (livePollTimer) clearInterval(livePollTimer);
            if (flightsPollTimer) clearInterval(flightsPollTimer);
            if (alertsPollTimer) clearInterval(alertsPollTimer);
            livePollTimer = null;
            flightsPollTimer = null;
            alertsPollTimer = null;

            fetchFlights();
            fetchAlerts();
            flightsPollTimer = setInterval(fetchFlights, 10000);
            alertsPollTimer = setInterval(fetchAlerts, 10000);

            if (portalView === 'live') {
                pollLiveTelemetry();
                livePollTimer = setInterval(pollLiveTelemetry, 2000);
            }
        }

        function switchSidebarTab(view) {
            sidebarView = view;
            document.getElementById('tab-flights').classList.toggle('active', view === 'flights');
            document.getElementById('tab-alerts').classList.toggle('active', view === 'alerts');
            document.getElementById('panel-flights').classList.toggle('active', view === 'flights');
            document.getElementById('panel-alerts').classList.toggle('active', view === 'alerts');
        }

        function levelBadge(flight) {
            if (flight.is_live) return '<span class="level-badge live">Live</span>';
            const level = (flight.level || '').toLowerCase();
            if (level === 'warn') return '<span class="level-badge warn">Warn</span>';
            if (level === 'alert') return '<span class="level-badge alert">Alert</span>';
            return '<span class="level-badge done">Done</span>';
        }

        function isFlightLive(flight) {
            return !!flight.is_live;
        }

        function createPlaneIcon(heading, isSelected, isLive, level) {
            const alertLevel = (level || '').toLowerCase();
            let fill, stroke, size, opacity, extraStyle = '';

            if (alertLevel === 'alert') {
                fill = '#ef4444';
                stroke = '#7f1d1d';
                size = isSelected ? 30 : 28;
                opacity = 1.0;
                if (isSelected) {
                    extraStyle = 'filter: drop-shadow(0 0 4px #ef4444) drop-shadow(0 0 8px rgba(239, 68, 68, 0.5));';
                }
            } else if (isSelected) {
                fill = '#ffffff';
                stroke = '#3b82f6';
                size = 30;
                opacity = 1.0;
                extraStyle = 'filter: drop-shadow(0 0 4px #3b82f6) drop-shadow(0 0 10px rgba(59, 130, 246, 0.55));';
            } else if (alertLevel === 'warn') {
                fill = '#f59e0b';
                stroke = '#78350f';
                size = 26;
                opacity = 1.0;
            } else if (isLive) {
                fill = '#10b981';
                stroke = '#064e3b';
                size = 24;
                opacity = 1.0;
            } else {
                fill = '#64748b';
                stroke = '#334155';
                size = 24;
                opacity = 0.65;
            }

            const svg = `
                <svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="${fill}" stroke="${stroke}" stroke-width="1" xmlns="http://www.w3.org/2000/svg" style="transform: rotate(${heading || 0}deg); transform-origin: center; opacity: ${opacity}; transition: transform 0.2s; ${extraStyle}">
                    <path d="M21 16v-2l-8-5V3.5c0-.83-.67-1.5-1.5-1.5S10 2.67 10 3.5V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L14 19v-5.5L21 16z"/>
                </svg>
            `;
            return L.divIcon({
                html: svg,
                className: 'plane-icon-div',
                iconSize: [size, size],
                iconAnchor: [size/2, size/2]
            });
        }

        async function fetchFlights() {
            try {
                const response = await fetch(`/api/flights?${viewQuery()}`);
                const data = await response.json();

                if (portalView === 'live') {
                    data.forEach(newFlight => {
                        const idx = flightsData.findIndex(f => f.flight_id === newFlight.flight_id);
                        if (idx !== -1) {
                            if (isFlightLive(flightsData[idx])) {
                                flightsData[idx].callsign = newFlight.callsign || flightsData[idx].callsign;
                                flightsData[idx].model = newFlight.model || flightsData[idx].model;
                                flightsData[idx].aircraft_type = newFlight.aircraft_type || flightsData[idx].aircraft_type;
                                flightsData[idx].owner = newFlight.owner || flightsData[idx].owner;
                                flightsData[idx].country = newFlight.country || flightsData[idx].country;
                                flightsData[idx].zone = newFlight.zone;
                                flightsData[idx].level = newFlight.level;
                            } else {
                                flightsData[idx] = newFlight;
                            }
                        } else {
                            flightsData.push(newFlight);
                        }
                    });

                    const remoteFlightIds = new Set(data.map(f => f.flight_id));
                    flightsData = flightsData.filter(f => remoteFlightIds.has(f.flight_id) || isFlightLive(f) || f.flight_id === activeFlightId);
                } else {
                    flightsData = data;
                }

                flightsData.sort((a, b) => {
                    const aLive = isFlightLive(a);
                    const bLive = isFlightLive(b);
                    if (aLive && !bLive) return -1;
                    if (!aLive && bLive) return 1;
                    return (b.start_time || 0) - (a.start_time || 0);
                });

                if (portalView === 'live') {
                    document.getElementById('flight-count').innerText = data.filter(f => f.is_live).length;
                    let liveCount = 0;
                    data.forEach(f => { if (f.is_live) liveCount++; });
                    document.getElementById('live-count').innerText = liveCount;
                } else {
                    document.getElementById('flight-count').innerText = data.length;
                }
                renderFlightList();
                plotAllFlights();
                if (showAllPaths) syncPathsForFilteredFlights();
            } catch (err) {
                console.error("Failed to fetch flights", err);
            }
        }

        async function fetchAlerts() {
            try {
                const response = await fetch(`/api/alerts?${viewQuery()}`);
                alertsData = await response.json();
                document.getElementById('alert-count').innerText = alertsData.length;
                renderAlertList();
            } catch (err) {
                console.error("Failed to fetch alerts", err);
            }
        }

        function renderAlertList() {
            const list = document.getElementById('alert-list');
            list.innerHTML = '';
            alertsData.forEach(alert => {
                const item = document.createElement('li');
                item.className = 'alert-item';
                if (alert.alert_id === activeAlertId) item.className += ' active';
                const timeStr = new Date(alert.timestamp * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
                const levelClass = (alert.level || '').toLowerCase() === 'alert' ? 'alert-level-alert' : 'alert-level-warn';
                item.innerHTML = `
                    <div class="alert-meta">
                        <span class="flight-callsign ${levelClass}">${(alert.level || 'event').toUpperCase()} · ${alert.zone || 'zone'}</span>
                        <span class="flight-icao">${(alert.icao || '').toUpperCase()}</span>
                    </div>
                    <div class="flight-meta-row" style="margin-top:4px;">
                        <span class="flight-desc">${alert.callsign || 'UNKNOWN'}</span>
                        <span class="flight-time">${timeStr}</span>
                    </div>
                `;
                item.addEventListener('click', () => selectAlert(alert));
                list.appendChild(item);
            });
        }

        async function selectAlert(alert) {
            activeAlertId = alert.alert_id;
            renderAlertList();
            if (alert.latitude != null && alert.longitude != null) {
                map.setView([alert.latitude, alert.longitude], Math.max(map.getZoom(), 10));
            }
            if (alert.flight_id) {
                await selectFlight(alert.flight_id);
            }
        }

        let flightAlertsRequestId = 0;

        function formatAlertLevel(level) {
            return String(level || 'event');
        }

        function formatAlertEta(eta) {
            return (eta != null && Number.isFinite(Number(eta))) ? `${Math.round(Number(eta))} s` : 'N/A';
        }

        function formatAlertAltitude(altitude) {
            return (altitude != null && Number.isFinite(Number(altitude))) ? `${Math.round(Number(altitude))} m` : 'N/A';
        }

        async function fetchFlightAlerts(flightId) {
            const requestId = ++flightAlertsRequestId;
            const container = document.getElementById('alert-timeline-list');
            container.innerHTML = '<div style="color:#64748b;">Loading alerts...</div>';
            try {
                const response = await fetch(`/api/alerts?${viewQuery()}&flight_id=${encodeURIComponent(flightId)}`);
                if (requestId !== flightAlertsRequestId) return [];
                if (!response.ok) {
                    container.innerHTML = '<div style="color:#64748b;">Failed to load alerts.</div>';
                    return [];
                }
                const alerts = await response.json();
                if (requestId !== flightAlertsRequestId) return [];
                if (!Array.isArray(alerts)) {
                    container.innerHTML = '<div style="color:#64748b;">Failed to load alerts.</div>';
                    return [];
                }
                container.innerHTML = '';
                if (!alerts.length) {
                    container.innerHTML = '<div style="color:#64748b;">No alert events for this flight.</div>';
                    return [];
                }
                alerts.slice().reverse().forEach(alert => {
                    const item = document.createElement('div');
                    const level = formatAlertLevel(alert.level).toLowerCase();
                    item.className = `alert-timeline-item ${level}`;
                    const timeStr = new Date(Number(alert.timestamp) * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
                    item.innerHTML = `
                        <div><strong>${formatAlertLevel(alert.level).toUpperCase()}</strong> in ${alert.zone || 'zone'} at ${timeStr}</div>
                        <div style="color:#94a3b8; margin-top:4px;">Alt ${formatAlertAltitude(alert.altitude)} · ETA ${formatAlertEta(alert.eta)}</div>
                    `;
                    item.addEventListener('click', () => selectAlert(alert));
                    container.appendChild(item);
                });
                return alerts;
            } catch (err) {
                if (requestId !== flightAlertsRequestId) return [];
                console.error('Failed to load flight alerts', err);
                container.innerHTML = '<div style="color:#64748b;">Failed to load alerts.</div>';
                return [];
            }
        }

        function formatZoneLevel(zone, level) {
            const z = (zone || '').trim();
            const l = (level || '').trim();
            if (!z && !l) return 'N/A';
            return `${z || 'N/A'} / ${l || 'N/A'}`;
        }

        function clearEventMarkers(flightId) {
            if (!planeEventMarkers[flightId]) return;
            planeEventMarkers[flightId].forEach(marker => map.removeLayer(marker));
            delete planeEventMarkers[flightId];
        }

        function setEventMarkers(flightId, alerts) {
            clearEventMarkers(flightId);
            const markers = [];
            (alerts || []).forEach(alert => {
                if (alert.latitude == null || alert.longitude == null) return;
                const level = (alert.level || '').toLowerCase();
                const fillColor = level === 'alert' ? '#ef4444' : '#f59e0b';
                const marker = L.circleMarker([alert.latitude, alert.longitude], {
                    radius: 6,
                    color: '#fff',
                    weight: 2,
                    fillColor,
                    fillOpacity: 0.95,
                }).addTo(map);
                marker.bindTooltip(`${(alert.level || 'event').toUpperCase()} · ${alert.zone || 'zone'}`);
                marker.on('click', () => selectFlight(flightId));
                markers.push(marker);
            });
            if (markers.length) {
                planeEventMarkers[flightId] = markers;
            }
        }

        function bindPathClick(path, flightId) {
            path.off('click');
            path.on('click', () => selectFlight(flightId));
        }

        let warningFilter = 'all';

        function getFilteredFlights() {
            return flightsData.filter(flight => {
                const callsign = (flight.callsign || '').toLowerCase();
                const icao = (flight.icao || '').toLowerCase();
                const model = (flight.model || '').toLowerCase();
                const aircraftType = (flight.aircraft_type || flight.typecode || '').toLowerCase();
                const matchesSearch = callsign.includes(searchQuery) || icao.includes(searchQuery) || model.includes(searchQuery) || aircraftType.includes(searchQuery);
                
                let matchesWarning = true;
                const level = (flight.level || '').toLowerCase();
                if (warningFilter === 'warn') {
                    matchesWarning = (level === 'warn');
                } else if (warningFilter === 'alert') {
                    matchesWarning = (level === 'alert');
                } else if (warningFilter === 'any') {
                    matchesWarning = (level === 'warn' || level === 'alert');
                }
                
                return matchesSearch && matchesWarning;
            });
        }

        function renderFlightList() {
            const list = document.getElementById('flight-list');
            list.innerHTML = '';
            
            const filtered = getFilteredFlights();
            
            filtered.forEach(flight => {
                const item = document.createElement('li');
                item.className = 'flight-item';
                if (flight.flight_id === activeFlightId) {
                    item.className += ' active';
                }
                
                const startTime = new Date(flight.start_time * 1000).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
                const isLive = isFlightLive(flight);
                const statusDot = isLive ? '<span class="status-dot live"></span>' : '<span class="status-dot"></span>';
                
                item.innerHTML = `
                    <div class="flight-meta-row">
                        <span class="flight-callsign">${statusDot} ${flight.callsign || 'UNKNOWN'} ${levelBadge(flight)}</span>
                        <span class="flight-icao">${flight.icao.toUpperCase()}</span>
                    </div>
                    <div class="flight-meta-row" style="margin-top: 4px;">
                        <span class="flight-desc">${flight.model || 'Unknown Model'}</span>
                        <span class="flight-time">${startTime}</span>
                    </div>
                `;
                item.addEventListener('click', () => selectFlight(flight.flight_id));
                list.appendChild(item);
            });
        }

        function plotAllFlights() {
            const filtered = getFilteredFlights();
            const filteredFlightIds = new Set(filtered.map(f => f.flight_id));

            // Clean up markers for flights no longer in filtered list and not selected
            Object.keys(planeMarkers).forEach(flightId => {
                if (!filteredFlightIds.has(flightId) && flightId !== activeFlightId) {
                    map.removeLayer(planeMarkers[flightId]);
                    delete planeMarkers[flightId];
                }
            });

            // Plot filtered planes
            filtered.forEach(flight => {
                if (flight.latitude !== null && flight.longitude !== null && flight.latitude !== undefined && flight.longitude !== undefined) {
                    const pos = [flight.latitude, flight.longitude];
                    const isSelected = flight.flight_id === activeFlightId;
                    const isLive = isFlightLive(flight);

                    if (planeMarkers[flight.flight_id]) {
                        planeMarkers[flight.flight_id].setLatLng(pos);
                        planeMarkers[flight.flight_id].setIcon(createPlaneIcon(flight.heading, isSelected, isLive, flight.level));
                    } else {
                        const marker = L.marker(pos, {
                            icon: createPlaneIcon(flight.heading, isSelected, isLive, flight.level)
                        }).addTo(map);
                        marker.on('click', () => selectFlight(flight.flight_id));
                        planeMarkers[flight.flight_id] = marker;
                    }

                    if (isSelected && followSelectedPlane) {
                        followPlaneOnMap(flight.latitude, flight.longitude);
                    }
                }
            });
        }

        function updateMarkerIcons() {
            flightsData.forEach(flight => {
                const marker = planeMarkers[flight.flight_id];
                if (marker) {
                    const isSelected = flight.flight_id === activeFlightId;
                    const isLive = isFlightLive(flight);
                    marker.setIcon(createPlaneIcon(flight.heading, isSelected, isLive, flight.level));
                }
            });
        }

        async function selectFlight(flightId) {
            activeFlightId = flightId;
            followSelectedPlane = true;
            renderFlightList();
            updateMarkerIcons();
            updateFollowButton();
            
            // Draw or refresh flight path(s)
            await refreshFlightPaths({ fitSelected: true });

            // Open Drawer
            document.getElementById('details-drawer').classList.add('open');
            switchTab('alerts');
            
            // Load detail data (including raw messages)
            try {
                const response = await fetch(`/api/flight?${viewQuery()}&flight_id=${encodeURIComponent(flightId)}`);
                if (response.ok) {
                    const flightDetail = await response.json();
                    showDetails(flightDetail);
                }
            } catch (err) {
                console.error("Failed to fetch flight details", err);
            }
        }

        function pathStyleForFlight(flight, isSelected) {
            const alertLevel = (flight && flight.level || '').toLowerCase();
            let color = '#64748b';
            if (alertLevel === 'alert') color = '#ef4444';
            else if (alertLevel === 'warn') color = '#f59e0b';
            if (isSelected) color = '#3b82f6';
            return {
                color,
                weight: isSelected ? 3 : 2,
                opacity: isSelected ? 0.85 : 0.4,
            };
        }

        function removeFlightPath(flightId) {
            if (planePaths[flightId]) {
                map.removeLayer(planePaths[flightId]);
                delete planePaths[flightId];
            }
            clearEventMarkers(flightId);
        }

        function clearAllFlightPaths() {
            Object.values(planePaths).forEach(path => map.removeLayer(path));
            Object.values(planeEventMarkers).forEach(markers => markers.forEach(marker => map.removeLayer(marker)));
            planePaths = {};
            planeEventMarkers = {};
        }

        function updatePathStyles() {
            Object.keys(planePaths).forEach(flightId => {
                const flight = flightsData.find(f => f.flight_id === flightId);
                planePaths[flightId].setStyle(pathStyleForFlight(flight, flightId === activeFlightId));
            });
        }

        async function fetchAndSetPath(flightId, { isSelected = false } = {}) {
            try {
                const [telemetryResponse, alertsResponse] = await Promise.all([
                    fetch(`/api/telemetry?${viewQuery()}&flight_id=${encodeURIComponent(flightId)}`),
                    fetch(`/api/alerts?${viewQuery()}&flight_id=${encodeURIComponent(flightId)}`),
                ]);
                if (!telemetryResponse.ok) {
                    removeFlightPath(flightId);
                    return null;
                }
                const points = await telemetryResponse.json();
                const latlngs = points
                    .filter(p => p.latitude !== undefined && p.longitude !== undefined)
                    .map(p => [p.latitude, p.longitude]);
                if (latlngs.length === 0) {
                    removeFlightPath(flightId);
                    return null;
                }
                const flight = flightsData.find(f => f.flight_id === flightId);
                const style = {
                    ...pathStyleForFlight(flight, isSelected),
                    className: 'flight-path',
                };
                if (planePaths[flightId]) {
                    planePaths[flightId].setLatLngs(latlngs);
                    planePaths[flightId].setStyle(style);
                } else {
                    planePaths[flightId] = L.polyline(latlngs, style).addTo(map);
                }
                bindPathClick(planePaths[flightId], flightId);

                if (alertsResponse.ok) {
                    const alerts = await alertsResponse.json();
                    if (Array.isArray(alerts)) {
                        setEventMarkers(flightId, alerts);
                    }
                } else {
                    clearEventMarkers(flightId);
                }
                return planePaths[flightId];
            } catch (err) {
                console.error('Failed to fetch flight path', flightId, err);
                return null;
            }
        }

        async function syncPathsForFilteredFlights() {
            const filtered = getFilteredFlights();
            const visibleIds = new Set(filtered.map(f => f.flight_id));
            Object.keys(planePaths).forEach(id => {
                if (!visibleIds.has(id)) removeFlightPath(id);
            });
            const missing = filtered.filter(f => !planePaths[f.flight_id]);
            await Promise.all(missing.map(f =>
                fetchAndSetPath(f.flight_id, { isSelected: f.flight_id === activeFlightId })
            ));
            updatePathStyles();
        }

        async function refreshFlightPaths({ fitSelected = false } = {}) {
            if (showAllPaths) {
                await syncPathsForFilteredFlights();
            } else if (activeFlightId) {
                clearAllFlightPaths();
                await fetchAndSetPath(activeFlightId, { isSelected: true });
            } else {
                clearAllFlightPaths();
            }

            if (!fitSelected || !activeFlightId || !planePaths[activeFlightId]) return;

            const flight = flightsData.find(f => f.flight_id === activeFlightId);
            const isLive = flight && isFlightLive(flight);
            const latlngs = planePaths[activeFlightId].getLatLngs();
            const last = latlngs[latlngs.length - 1];
            if (followSelectedPlane && portalView === 'live' && isLive) {
                followPlaneOnMap(last.lat, last.lng, { initial: true });
            } else {
                map.fitBounds(planePaths[activeFlightId].getBounds(), { padding: [50, 50] });
            }
        }

        function showDetails(flightDetail) {
            const callsign = flightDetail.callsign || 'UNKNOWN';
            const icao = flightDetail.icao.toUpperCase();
            document.getElementById('detail-callsign').innerText = callsign;
            document.getElementById('detail-icao').innerText = icao;
            document.getElementById('detail-callsign-photo').innerText = callsign;
            document.getElementById('detail-icao-photo').innerText = icao;
            document.getElementById('detail-registration').innerText = flightDetail.registration || 'Unknown';
            document.getElementById('detail-model').innerText = flightDetail.model || 'Unknown Model';
            document.getElementById('detail-type').innerText = flightDetail.aircraft_type || flightDetail.typecode || 'Unknown Type';
            document.getElementById('detail-owner').innerText = flightDetail.owner || 'Unknown Owner';
            document.getElementById('detail-country').innerText = flightDetail.country || 'Unknown';
            document.getElementById('detail-zone-level').innerText = formatZoneLevel(flightDetail.zone, flightDetail.level);
            const zoneLevelEl = document.getElementById('detail-zone-level');
            const hasAlert = Boolean((flightDetail.zone || '').trim() || (flightDetail.level || '').trim());
            zoneLevelEl.style.color = hasAlert ? '#f59e0b' : '#94a3b8';

            // Render photo if available
            const photoContainer = document.getElementById('detail-photo-container');
            const photoImg = document.getElementById('detail-photo');
            const photographerSpan = document.getElementById('detail-photo-photographer');
            const photoLink = document.getElementById('detail-photo-link');

            const photoUrl = typeof flightDetail.photo_url === 'string'
                ? flightDetail.photo_url
                : (flightDetail.photo_url && flightDetail.photo_url.src) || null;
            const drawerHeader = document.getElementById('drawer-header');
            const detailsDrawer = document.getElementById('details-drawer');
            if (photoUrl) {
                photoImg.src = photoUrl;
                photographerSpan.innerText = flightDetail.photo_photographer || 'Unknown';
                photoLink.href = flightDetail.photo_link || '#';
                photoContainer.style.display = 'block';
                drawerHeader.classList.add('has-photo');
                drawerHeader.classList.remove('no-photo');
                detailsDrawer.classList.add('has-photo-drawer');
            } else {
                photoImg.src = '';
                photoContainer.style.display = 'none';
                drawerHeader.classList.remove('has-photo');
                drawerHeader.classList.add('no-photo');
                detailsDrawer.classList.remove('has-photo-drawer');
            }

            // Render raw messages
            const rawList = document.getElementById('raw-messages-list');
            rawList.innerHTML = '';
            
            const rawMsgs = flightDetail.raw_messages || [];
            if (rawMsgs.length === 0) {
                rawList.innerHTML = '<div class="terminal-line" style="color: #64748b;">No raw messages captured yet.</div>';
            } else {
                rawMsgs.forEach(msg => {
                    const line = document.createElement('div');
                    line.className = 'terminal-line';
                    const msgTime = new Date(msg.timestamp * 1000).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit', second:'2-digit'});
                    line.innerHTML = `
                        <span class="terminal-time">[${msgTime}]</span>
                        <span class="terminal-hex">${msg.hex.toUpperCase()}</span>
                    `;
                    rawList.appendChild(line);
                });
                rawList.scrollTop = rawList.scrollHeight;
            }

            // Populate telemetry table log and alert timeline
            fetchTelemetryTable(flightDetail.flight_id);
            fetchFlightAlerts(flightDetail.flight_id).then(alerts => {
                if (planePaths[flightDetail.flight_id]) {
                    setEventMarkers(flightDetail.flight_id, alerts);
                }
            });
        }

        async function fetchTelemetryTable(flightId) {
            try {
                const response = await fetch(`/api/telemetry?${viewQuery()}&flight_id=${encodeURIComponent(flightId)}`);
                const points = await response.json();
                
                const tableBody = document.getElementById('telemetry-table-body');
                tableBody.innerHTML = '';

                if (points.length === 0) {
                    tableBody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: #64748b;">No telemetry data.</td></tr>';
                } else {
                    points.forEach(point => {
                        const row = document.createElement('tr');
                        const timeStr = new Date(point.timestamp * 1000).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit', second:'2-digit'});
                        
                        const altStr = point.altitude !== null ? `${Math.round(point.altitude)} m` : 'N/A';
                        const speedStr = point.speed !== null ? `${Math.round(point.speed)} km/h` : 'N/A';
                        const headingStr = point.heading !== null ? `${Math.round(point.heading)}°` : 'N/A';
                        
                        row.innerHTML = `
                            <td>${timeStr}</td>
                            <td>${altStr}</td>
                            <td>${speedStr}</td>
                            <td>${headingStr}</td>
                        `;
                        tableBody.appendChild(row);
                    });

                    // Update drawer real-time telemetry details with the latest point
                    const lastPoint = points[points.length - 1];
                    document.getElementById('detail-altitude').innerText = lastPoint.altitude !== null ? `${Math.round(lastPoint.altitude)} m (${Math.round(lastPoint.altitude * 3.28084)} ft)` : 'N/A';
                    document.getElementById('detail-speed').innerText = lastPoint.speed !== null ? `${Math.round(lastPoint.speed)} km/h (${Math.round(lastPoint.speed * 0.539957)} kt)` : 'N/A';
                    document.getElementById('detail-heading').innerText = lastPoint.heading !== null ? `${Math.round(lastPoint.heading)}°` : 'N/A';
                }
            } catch (err) {
                console.error("Failed to populate telemetry table log", err);
            }
        }

        function switchTab(tabName) {
            const tabButtons = document.querySelectorAll('.tab-btn');
            tabButtons.forEach(btn => {
                if (btn.id === `tab-btn-${tabName}`) {
                    btn.classList.add('active');
                } else {
                    btn.classList.remove('active');
                }
            });

            const contents = document.querySelectorAll('.tab-content');
            contents.forEach(content => {
                if (content.id === `tab-${tabName}`) {
                    content.style.display = 'flex';
                } else {
                    content.style.display = 'none';
                }
            });
        }

        async function pollLiveTelemetry() {
            if (portalView !== 'live') return;
            try {
                const response = await fetch(`/api/live?since=${lastSeenTimestamp}`);
                const data = await response.json();
                lastSeenTimestamp = data.timestamp;

                const points = data.telemetry;
                
                points.forEach(point => {
                    if (point.latitude !== undefined && point.longitude !== undefined) {
                        const flightId = point.flight_id;
                        const pos = [point.latitude, point.longitude];
                        const isSelected = flightId === activeFlightId;
                        
                        let flight = flightsData.find(f => f.flight_id === flightId);
                        if (!flight) {
                            flight = {
                                flight_id: flightId,
                                icao: point.icao,
                                callsign: null,
                                model: null,
                                aircraft_type: null,
                                owner: null,
                                country: null,
                                zone: '',
                                level: '',
                                start_time: point.timestamp,
                                end_time: point.timestamp,
                                latitude: point.latitude,
                                longitude: point.longitude,
                                heading: point.heading,
                                altitude: point.altitude,
                                speed: point.speed,
                                is_live: true
                            };
                            flightsData.push(flight);
                            renderFlightList();
                        } else {
                            flight.latitude = point.latitude;
                            flight.longitude = point.longitude;
                            flight.altitude = point.altitude;
                            flight.speed = point.speed;
                            flight.heading = point.heading;
                            flight.end_time = point.timestamp;
                            flight.is_live = true;
                        }

                        if (planeMarkers[flightId]) {
                            planeMarkers[flightId].setLatLng(pos);
                            planeMarkers[flightId].setIcon(createPlaneIcon(point.heading, isSelected, true, flight.level));
                        } else {
                            const marker = L.marker(pos, {
                                icon: createPlaneIcon(point.heading, isSelected, true, flight.level)
                            }).addTo(map);
                            marker.on('click', () => selectFlight(flightId));
                            planeMarkers[flightId] = marker;
                        }

                        if (planePaths[flightId]) {
                            planePaths[flightId].addLatLng(pos);
                        } else if (showAllPaths) {
                            fetchAndSetPath(flightId, { isSelected });
                        }

                        if (isSelected) {
                            if (followSelectedPlane) {
                                followPlaneOnMap(point.latitude, point.longitude);
                            }
                            fetch(`/api/flight?${viewQuery()}&flight_id=${encodeURIComponent(flightId)}`)
                                .then(res => res.json())
                                .then(detail => showDetails(detail));
                        }
                    }
                });

                // Update live count
                let liveCount = 0;
                flightsData.forEach(f => {
                    if (isFlightLive(f)) liveCount++;
                });
                document.getElementById('live-count').innerText = liveCount;
            } catch (err) {
                console.error("Failed to poll live telemetry", err);
            }
        }

        // Close drawer trigger
        document.getElementById('close-drawer-btn').addEventListener('click', () => {
            document.getElementById('details-drawer').classList.remove('open');
            activeFlightId = null;
            followSelectedPlane = false;
            updateFollowButton();
            renderFlightList();
            updateMarkerIcons();
            refreshFlightPaths();
        });

        document.getElementById('follow-btn').addEventListener('click', () => {
            if (!activeFlightId) return;
            if (followSelectedPlane) {
                followSelectedPlane = false;
            } else {
                enableFollowPlane();
            }
            updateFollowButton();
        });

        document.getElementById('zones-btn').addEventListener('click', async () => {
            zonesVisible = !zonesVisible;
            updateZonesButton();
            if (zonesVisible) {
                await fetchZones();
            } else {
                clearZoneLayers();
            }
        });

        document.getElementById('paths-btn').addEventListener('click', async () => {
            showAllPaths = !showAllPaths;
            updatePathsButton();
            await refreshFlightPaths({ fitSelected: !!activeFlightId });
        });

        document.getElementById('zoom-in-btn').addEventListener('click', () => map.zoomIn());
        document.getElementById('zoom-out-btn').addEventListener('click', () => map.zoomOut());

        // Search trigger
        document.getElementById('search-input').addEventListener('input', (e) => {
            searchQuery = e.target.value.toLowerCase();
            renderFlightList();
            plotAllFlights();
            if (showAllPaths) syncPathsForFilteredFlights();
        });

        // Warning filter trigger
        document.getElementById('warning-filter').addEventListener('change', (e) => {
            warningFilter = e.target.value;
            renderFlightList();
            plotAllFlights();
            if (showAllPaths) syncPathsForFilteredFlights();
        });

        async function init() {
            updateZonesButton();
            updatePathsButton();
            await fetchZones();
            restartPolling();
        }

        init();
    </script>
</body>
</html>
"""
