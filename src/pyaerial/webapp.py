"""
Lightweight webapp server for live flight tracking.

Reads live flights from Redis and retained historical flights from MongoDB.
"""
from __future__ import annotations

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pymongo

from pyaerial.calc.aircraft_db import AircraftDB
from pyaerial.config import load_config
from pyaerial.constants import DEFAULT_AIRCRAFT_DB
from pyaerial.store.redis_live import RedisLiveStore

log = logging.getLogger("pyaerial.webapp")

_FLIGHT_STATUS_LIVE = "live"


def _connect_stores(config_path: str) -> tuple[pymongo.MongoClient, pymongo.database.Database, RedisLiveStore]:
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
    return client, db, live_store


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
        coords = doc["position"]["coordinates"]
        point["longitude"] = coords[0]
        point["latitude"] = coords[1]
    return point


def _format_alert(doc: dict[str, Any]) -> dict[str, Any]:
    if "alert_id" in doc:
        coords = doc.get("position", {}).get("coordinates", [None, None])
        return {
            "alert_id": doc["alert_id"],
            "flight_id": doc.get("flight_id"),
            "icao": doc.get("icao"),
            "callsign": doc.get("callsign"),
            "zone": doc.get("zone"),
            "level": doc.get("level"),
            "timestamp": doc.get("timestamp"),
            "eta": doc.get("eta"),
            "altitude": doc.get("altitude"),
            "latitude": coords[1] if len(coords) > 1 else None,
            "longitude": coords[0] if coords else None,
        }
    coords = doc.get("position", {}).get("coordinates", [None, None])
    return {
        "alert_id": str(doc["_id"]),
        "flight_id": doc.get("flight_id"),
        "icao": doc.get("icao"),
        "callsign": doc.get("callsign"),
        "zone": doc.get("zone"),
        "level": doc.get("level"),
        "timestamp": doc.get("timestamp"),
        "eta": doc.get("eta"),
        "altitude": doc.get("altitude"),
        "latitude": coords[1] if len(coords) > 1 else None,
        "longitude": coords[0] if coords else None,
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

    def send_json(self, data: Any):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))


def run_webapp(config_path: str = "config.yaml", *,
               aircraft_db_path: str = DEFAULT_AIRCRAFT_DB,
               host: str = "0.0.0.0", port: int = 10090) -> None:
    client, db, live_store = _connect_stores(config_path)
    aircraft_db = AircraftDB(aircraft_db_path) if aircraft_db_path else None

    server = ThreadingHTTPServer((host, port), WebAppHandler)
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
            justify-content: space-between;
        }
        .stat-card {
            background-color: #222;
            padding: 4px 8px;
            border-radius: 4px;
            border: 1px solid #2d2d2d;
            display: flex;
            align-items: center;
            gap: 6px;
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
            background-color: #1e2638;
            border-left: 3px solid #3b82f6;
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
            transition: color 0.15s;
        }
        .close-btn:hover {
            color: #ef4444;
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
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            font-size: 0.75rem;
        }
        .details-label {
            color: #888;
        }
        .details-value {
            color: #fff;
            text-align: right;
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
            padding: 10px;
            flex-grow: 1;
            min-height: 220px;
            max-height: 340px;
            overflow-y: auto;
            color: #38bdf8;
        }
        .terminal-line {
            display: flex;
            margin-bottom: 4px;
            line-height: 1.4;
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
        <div id="map"></div>
        
        <!-- Sliding Details Drawer -->
        <div id="details-drawer">
            <div id="drawer-header">
                <button id="close-drawer-btn" class="close-btn">&times;</button>
                <div class="flight-desc" style="text-transform: uppercase; letter-spacing: 0.1em; color: #94a3b8; font-weight: 600; font-size: 0.75rem;">Selected Aircraft</div>
                <h2><span id="detail-callsign">N/A</span> <span id="detail-icao" class="flight-icao">N/A</span></h2>
            </div>
            <div class="drawer-content">
                <div class="info-section">
                    <h3>Aircraft Details</h3>
                    <div class="details-grid">
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
                    <button class="tab-btn active" id="tab-btn-raw" onclick="switchTab('raw')">Raw Messages</button>
                    <button class="tab-btn" id="tab-btn-telemetry" onclick="switchTab('telemetry')">Telemetry Log</button>
                    <button class="tab-btn" id="tab-btn-alerts" onclick="switchTab('alerts')">Alert Timeline</button>
                </div>
                
                <div class="tab-content" id="tab-raw">
                    <div id="raw-messages-list" class="terminal-list">
                        <!-- Dynamic terminal hex rows -->
                    </div>
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

                <div class="tab-content" id="tab-alerts" style="display: none;">
                    <div id="alert-timeline-list"></div>
                </div>
            </div>
        </div>
    </div>

    <!-- Leaflet JS -->
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script>
        const map = L.map('map').setView([36.681, -78.875], 8);

        L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
            attribution: '&copy; OpenStreetMap &copy; CARTO'
        }).addTo(map);

        let planeMarkers = {}; 
        let planePaths = {};   
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
            lastSeenTimestamp = 0;
            flightsData = [];
            alertsData = [];
            Object.values(planeMarkers).forEach(marker => map.removeLayer(marker));
            Object.values(planePaths).forEach(path => map.removeLayer(path));
            planeMarkers = {};
            planePaths = {};
            document.getElementById('details-drawer').classList.remove('open');
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

        function createPlaneIcon(heading, isSelected, isLive) {
            const fill = isSelected ? '#f43f5e' : (isLive ? '#10b981' : '#64748b');
            const stroke = isSelected ? '#9f1239' : (isLive ? '#064e3b' : '#334155');
            const size = isSelected ? 30 : 24;
            const opacity = isLive ? 1.0 : 0.65;
            
            const svg = `
                <svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="${fill}" stroke="${stroke}" stroke-width="1" xmlns="http://www.w3.org/2000/svg" style="transform: rotate(${heading || 0}deg); transform-origin: center; opacity: ${opacity}; transition: transform 0.2s;">
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

        async function fetchFlightAlerts(flightId) {
            const container = document.getElementById('alert-timeline-list');
            container.innerHTML = '<div style="color:#64748b;">Loading alerts...</div>';
            try {
                const response = await fetch(`/api/alerts?${viewQuery()}&flight_id=${encodeURIComponent(flightId)}`);
                const alerts = await response.json();
                container.innerHTML = '';
                if (!alerts.length) {
                    container.innerHTML = '<div style="color:#64748b;">No alert events for this flight.</div>';
                    return;
                }
                alerts.reverse().forEach(alert => {
                    const item = document.createElement('div');
                    const level = (alert.level || '').toLowerCase();
                    item.className = `alert-timeline-item ${level}`;
                    const timeStr = new Date(alert.timestamp * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
                    item.innerHTML = `
                        <div><strong>${(alert.level || 'event').toUpperCase()}</strong> in ${alert.zone || 'zone'} at ${timeStr}</div>
                        <div style="color:#94a3b8; margin-top:4px;">Alt ${alert.altitude != null ? Math.round(alert.altitude) + ' m' : 'N/A'} · ETA ${alert.eta != null ? Math.round(alert.eta) + ' s' : 'N/A'}</div>
                    `;
                    item.addEventListener('click', () => selectAlert(alert));
                    container.appendChild(item);
                });
            } catch (err) {
                container.innerHTML = '<div style="color:#64748b;">Failed to load alerts.</div>';
            }
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
                        planeMarkers[flight.flight_id].setIcon(createPlaneIcon(flight.heading, isSelected, isLive));
                    } else {
                        const marker = L.marker(pos, {
                            icon: createPlaneIcon(flight.heading, isSelected, isLive)
                        }).addTo(map);
                        marker.on('click', () => selectFlight(flight.flight_id));
                        planeMarkers[flight.flight_id] = marker;
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
                    marker.setIcon(createPlaneIcon(flight.heading, isSelected, isLive));
                }
            });
        }

        async function selectFlight(flightId) {
            activeFlightId = flightId;
            renderFlightList();
            updateMarkerIcons();
            
            // Draw Flight Path
            drawFlightPath(flightId);

            // Open Drawer
            document.getElementById('details-drawer').classList.add('open');
            
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

        async function drawFlightPath(flightId) {
            // Remove previous paths
            Object.values(planePaths).forEach(path => map.removeLayer(path));
            planePaths = {};

            try {
                const response = await fetch(`/api/telemetry?${viewQuery()}&flight_id=${encodeURIComponent(flightId)}`);
                const points = await response.json();
                
                if (points.length > 0) {
                    const latlngs = points
                        .filter(p => p.latitude !== undefined && p.longitude !== undefined)
                        .map(p => [p.latitude, p.longitude]);
                    
                    if (latlngs.length > 0) {
                        const path = L.polyline(latlngs, {color: '#f43f5e', weight: 3, opacity: 0.85}).addTo(map);
                        planePaths[flightId] = path;
                        
                        // Center map on path
                        map.fitBounds(path.getBounds(), {padding: [50, 50]});
                    }
                }
            } catch (err) {
                console.error("Failed to fetch flight path telemetry", err);
            }
        }

        function showDetails(flightDetail) {
            document.getElementById('detail-callsign').innerText = flightDetail.callsign || 'UNKNOWN';
            document.getElementById('detail-icao').innerText = flightDetail.icao.toUpperCase();
            document.getElementById('detail-model').innerText = flightDetail.model || 'Unknown Model';
            document.getElementById('detail-type').innerText = flightDetail.aircraft_type || flightDetail.typecode || 'Unknown Type';
            document.getElementById('detail-owner').innerText = flightDetail.owner || 'Unknown Owner';
            document.getElementById('detail-country').innerText = flightDetail.country || 'Unknown';
            document.getElementById('detail-zone-level').innerText = `${flightDetail.zone} / ${flightDetail.level}`;

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
            fetchFlightAlerts(flightDetail.flight_id);
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
                            planeMarkers[flightId].setIcon(createPlaneIcon(point.heading, isSelected, true));
                        } else {
                            const marker = L.marker(pos, {
                                icon: createPlaneIcon(point.heading, isSelected, true)
                            }).addTo(map);
                            marker.on('click', () => selectFlight(flightId));
                            planeMarkers[flightId] = marker;
                        }

                        if (isSelected) {
                            if (planePaths[flightId]) {
                                planePaths[flightId].addLatLng(pos);
                            }
                            // Refresh detail drawer live
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
            renderFlightList();
            updateMarkerIcons();
            Object.values(planePaths).forEach(path => map.removeLayer(path));
            planePaths = {};
        });

        // Search trigger
        document.getElementById('search-input').addEventListener('input', (e) => {
            searchQuery = e.target.value.toLowerCase();
            renderFlightList();
            plotAllFlights();
        });

        // Warning filter trigger
        document.getElementById('warning-filter').addEventListener('change', (e) => {
            warningFilter = e.target.value;
            renderFlightList();
            plotAllFlights();
        });

        async function init() {
            restartPolling();
        }

        init();
    </script>
</body>
</html>
"""
