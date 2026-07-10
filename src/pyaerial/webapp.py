"""
Lightweight webapp server for live flight tracking.

Serves an interactive map showing real-time aircraft positions and paths,
polling telemetry from the consolidated MongoDB collections.
"""
from __future__ import annotations

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import pymongo

from pyaerial.calc.aircraft_db import AircraftDB
from pyaerial.config import load_config
from pyaerial.constants import DEFAULT_AIRCRAFT_DB

log = logging.getLogger("pyaerial.webapp")


class WebAppHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Mute standard HTTP request logging in console unless in debug mode
        log.debug(format, *args)

    @property
    def db(self) -> pymongo.database.Database:
        return self.server.db

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
            self.handle_api_flights()
        elif path == "/api/flight":
            self.handle_api_flight(query)
        elif path == "/api/telemetry":
            self.handle_api_telemetry(query)
        elif path == "/api/live":
            self.handle_api_live(query)
        else:
            self.send_error(404, "Not Found")

    def handle_api_flights(self):
        try:
            cursor = self.db.get_collection("flights").find().sort("start_time", -1).limit(50)
            flights = []
            for doc in cursor:
                icao = doc.get("icao", "")
                meta = self.aircraft_db.lookup(icao) if self.aircraft_db else None
                
                # Fetch latest telemetry point for this flight to get current/last position
                last_tel = self.db.get_collection("telemetry").find_one(
                    {"flight_id": doc["_id"]},
                    sort=[("timestamp", pymongo.DESCENDING)]
                )
                
                lat, lon, alt, speed, heading = None, None, None, None, None
                if last_tel:
                    alt = last_tel.get("altitude")
                    speed = last_tel.get("horizontal_speed")
                    heading = last_tel.get("direction")
                    if "position" in last_tel:
                        coords = last_tel["position"]["coordinates"]
                        lon = coords[0]
                        lat = coords[1]

                flights.append({
                    "flight_id": doc["_id"],
                    "icao": icao,
                    "zone": doc.get("zone"),
                    "level": doc.get("level"),
                    "start_time": doc.get("start_time"),
                    "end_time": doc.get("end_time"),
                    "callsign": doc.get("info", {}).get("callsign") or (meta.get("callsign") if meta else None),
                    "model": doc.get("info", {}).get("model") or (meta.get("model") if meta else None),
                    "owner": doc.get("info", {}).get("owner") or (meta.get("owner") if meta else None),
                    "country": doc.get("info", {}).get("country") or (meta.get("country") if meta else None),
                    "latitude": lat,
                    "longitude": lon,
                    "altitude": alt,
                    "speed": speed,
                    "heading": heading,
                })
            self.send_json(flights)
        except Exception as exc:
            self.send_error(500, f"Database error: {exc}")

    def handle_api_flight(self, query: dict[str, list[str]]):
        flight_ids = query.get("flight_id", [])
        if not flight_ids:
            self.send_error(400, "Missing flight_id parameter")
            return
        flight_id = flight_ids[0]
        try:
            doc = self.db.get_collection("flights").find_one({"_id": flight_id})
            if not doc:
                self.send_error(404, "Flight not found")
                return
            icao = doc.get("icao", "")
            meta = self.aircraft_db.lookup(icao) if self.aircraft_db else None
            flight_data = {
                "flight_id": doc["_id"],
                "icao": icao,
                "zone": doc.get("zone"),
                "level": doc.get("level"),
                "start_time": doc.get("start_time"),
                "end_time": doc.get("end_time"),
                "callsign": doc.get("info", {}).get("callsign") or (meta.get("callsign") if meta else None),
                "model": doc.get("info", {}).get("model") or (meta.get("model") if meta else None),
                "owner": doc.get("info", {}).get("owner") or (meta.get("owner") if meta else None),
                "country": doc.get("info", {}).get("country") or (meta.get("country") if meta else None),
                "raw_messages": doc.get("raw_messages", []),
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
        try:
            cursor = self.db.get_collection("telemetry").find({"flight_id": flight_id}).sort("timestamp", 1)
            points = []
            for doc in cursor:
                point = {
                    "timestamp": doc["timestamp"],
                    "altitude": doc.get("altitude"),
                    "speed": doc.get("horizontal_speed"),
                    "heading": doc.get("direction"),
                }
                if "position" in doc:
                    coords = doc["position"]["coordinates"]
                    point["longitude"] = coords[0]
                    point["latitude"] = coords[1]
                points.append(point)
            self.send_json(points)
        except Exception as exc:
            self.send_error(500, f"Database error: {exc}")

    def handle_api_live(self, query: dict[str, list[str]]):
        since_vals = query.get("since", [])
        since = float(since_vals[0]) if since_vals else 0.0
        now = time.time()
        try:
            cursor = self.db.get_collection("telemetry").find({"timestamp": {"$gt": since}}).sort("timestamp", 1)
            points = []
            for doc in cursor:
                point = {
                    "flight_id": doc["flight_id"],
                    "icao": doc["icao"],
                    "timestamp": doc["timestamp"],
                    "altitude": doc.get("altitude"),
                    "speed": doc.get("horizontal_speed"),
                    "heading": doc.get("direction"),
                }
                if "position" in doc:
                    coords = doc["position"]["coordinates"]
                    point["longitude"] = coords[0]
                    point["latitude"] = coords[1]
                points.append(point)
            self.send_json({
                "telemetry": points,
                "timestamp": now
            })
        except Exception as exc:
            self.send_error(500, f"Database error: {exc}")

    def send_json(self, data: any):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))


def run_webapp(config_path: str = "config.yaml", *,
               aircraft_db_path: str = DEFAULT_AIRCRAFT_DB,
               host: str = "0.0.0.0", port: int = 10090) -> None:
    config = load_config(config_path)
    client = pymongo.MongoClient(config.general.mongodb)

    try:
        db = client.get_default_database()
    except Exception:
        db = client.get_database("pyaerial")

    aircraft_db = AircraftDB(aircraft_db_path) if aircraft_db_path else None

    # ThreadingHTTPServer handles multiple concurrent HTTP queries asynchronously
    server = ThreadingHTTPServer((host, port), WebAppHandler)
    server.db = db
    server.aircraft_db = aircraft_db

    actual_host, actual_port = server.server_address
    print(f"Starting PyAerial live web app on http://localhost:{actual_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web server...")
    finally:
        server.server_close()
        client.close()
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
            background-color: #0c0e12;
            color: #f1f5f9;
            display: flex;
            height: 100vh;
            overflow: hidden;
        }
        #sidebar {
            width: 380px;
            background-color: #11141a;
            border-right: 1px solid #1e293b;
            display: flex;
            flex-direction: column;
            height: 100%;
            z-index: 10;
        }
        #sidebar-header {
            padding: 24px;
            border-bottom: 1px solid #1e293b;
            background: linear-gradient(135deg, #161a22 0%, #11141a 100%);
        }
        #sidebar-header h1 {
            font-size: 1.4rem;
            font-weight: 700;
            background: linear-gradient(90deg, #6366f1 0%, #a855f7 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 4px;
        }
        #sidebar-header p {
            font-size: 0.8rem;
            color: #94a3b8;
        }
        #search-container {
            padding: 12px 24px;
            border-bottom: 1px solid #1e293b;
            background-color: #13171f;
        }
        #search-input {
            width: 100%;
            padding: 10px 14px;
            border-radius: 8px;
            border: 1px solid #334155;
            background-color: #0f1218;
            color: #f1f5f9;
            font-family: inherit;
            font-size: 0.85rem;
            transition: all 0.2s;
        }
        #search-input:focus {
            outline: none;
            border-color: #6366f1;
            box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.2);
        }
        #stats-panel {
            padding: 14px 24px;
            background-color: #13171f;
            border-bottom: 1px solid #1e293b;
            font-size: 0.85rem;
            display: flex;
            justify-content: space-between;
        }
        .stat-card {
            background-color: #1a1f2c;
            padding: 6px 12px;
            border-radius: 6px;
            border: 1px solid #334155;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        #flight-list {
            flex-grow: 1;
            overflow-y: auto;
            list-style: none;
            padding: 12px;
        }
        .flight-item {
            padding: 14px 16px;
            border-radius: 10px;
            margin-bottom: 8px;
            border: 1px solid transparent;
            background-color: #161a23;
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .flight-item:hover {
            background-color: #1e2433;
            transform: translateX(4px);
            border-color: #334155;
        }
        .flight-item.active {
            background: linear-gradient(135deg, #1e2942 0%, #1a2235 100%);
            border-color: #6366f1;
            box-shadow: 0 4px 12px rgba(99, 102, 241, 0.15);
        }
        .flight-meta-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .flight-callsign {
            font-weight: 600;
            font-size: 0.95rem;
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
            box-shadow: 0 0 8px #10b981;
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
            70% { transform: scale(1); box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }
            100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }
        .flight-icao {
            font-family: 'JetBrains Mono', monospace;
            background-color: #2e384e;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.75rem;
            color: #cbd5e1;
            font-weight: 500;
        }
        .flight-desc {
            font-size: 0.8rem;
            color: #94a3b8;
        }
        .flight-time {
            font-size: 0.75rem;
            color: #64748b;
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
            background-color: #0b0d10;
        }
        /* Details Drawer Styling */
        #details-drawer {
            position: absolute;
            top: 0;
            right: -440px;
            width: 440px;
            height: 100%;
            background-color: #11141a;
            border-left: 1px solid #1e293b;
            box-shadow: -10px 0 30px rgba(0,0,0,0.6);
            z-index: 1010;
            transition: right 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            display: flex;
            flex-direction: column;
        }
        #details-drawer.open {
            right: 0;
        }
        #drawer-header {
            padding: 24px;
            border-bottom: 1px solid #1e293b;
            background: linear-gradient(135deg, #161a22 0%, #11141a 100%);
            position: relative;
        }
        #drawer-header h2 {
            font-size: 1.3rem;
            font-weight: 700;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 10px;
            margin-top: 8px;
        }
        .close-btn {
            background: none;
            border: none;
            color: #64748b;
            font-size: 1.8rem;
            cursor: pointer;
            position: absolute;
            top: 16px;
            right: 20px;
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
            padding: 20px 24px;
            border-bottom: 1px solid #1e293b;
        }
        .info-section h3 {
            font-size: 0.85rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: #6366f1;
            margin-bottom: 12px;
            font-weight: 600;
        }
        .details-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            font-size: 0.85rem;
        }
        .details-label {
            color: #94a3b8;
        }
        .details-value {
            color: #f1f5f9;
            text-align: right;
            font-weight: 500;
        }
        .drawer-tabs {
            display: flex;
            border-bottom: 1px solid #1e293b;
            background-color: #0f1218;
        }
        .tab-btn {
            flex: 1;
            padding: 14px;
            background: none;
            border: none;
            border-bottom: 2px solid transparent;
            color: #94a3b8;
            font-size: 0.85rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            font-family: inherit;
        }
        .tab-btn:hover {
            color: #fff;
            background-color: #141822;
        }
        .tab-btn.active {
            color: #6366f1;
            border-bottom-color: #6366f1;
            background-color: #11141a;
        }
        .tab-content {
            padding: 20px;
            flex-grow: 1;
            overflow-y: auto;
            background-color: #0d0f14;
            display: flex;
            flex-direction: column;
        }
        .terminal-list {
            background-color: #07080a;
            border: 1px solid #1e293b;
            border-radius: 8px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.8rem;
            padding: 12px;
            flex-grow: 1;
            min-height: 250px;
            max-height: 380px;
            overflow-y: auto;
            color: #38bdf8;
            box-shadow: inset 0 2px 8px rgba(0,0,0,0.8);
        }
        .terminal-line {
            display: flex;
            margin-bottom: 4px;
            line-height: 1.4;
        }
        .terminal-time {
            color: #475569;
            margin-right: 12px;
            user-select: none;
        }
        .terminal-hex {
            color: #34d399;
            font-weight: 500;
        }
        .table-container {
            border: 1px solid #1e293b;
            border-radius: 8px;
            overflow: hidden;
            background-color: #07080a;
            max-height: 380px;
            overflow-y: auto;
        }
        .tel-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.8rem;
            text-align: left;
        }
        .tel-table th, .tel-table td {
            padding: 10px 12px;
            border-bottom: 1px solid #1e293b;
        }
        .tel-table th {
            background-color: #0f1218;
            color: #94a3b8;
            font-weight: 600;
        }
        .tel-table td {
            color: #cbd5e1;
        }
        .plane-icon-div {
            background: none;
            border: none;
        }
        /* Custom scrollbar */
        ::-webkit-scrollbar {
            width: 6px;
        }
        ::-webkit-scrollbar-track {
            background: #11141a;
        }
        ::-webkit-scrollbar-thumb {
            background: #334155;
            border-radius: 3px;
        }
        ::-webkit-scrollbar-thumb:hover {
            background: #475569;
        }
    </style>
</head>
<body>
    <div id="sidebar">
        <div id="sidebar-header">
            <h1>PyAerial Live Tracker</h1>
            <p>Real-time Mode S & ADS-B aircraft flight analysis</p>
        </div>
        <div id="search-container">
            <input type="text" id="search-input" placeholder="Search by callsign, ICAO, or model..." />
        </div>
        <div id="stats-panel">
            <div class="stat-card">
                <span>Recent:</span>
                <strong id="flight-count" style="color: #6366f1;">0</strong>
            </div>
            <div class="stat-card">
                <span>Live:</span>
                <strong id="live-count" style="color: #10b981;">0</strong>
            </div>
        </div>
        <ul id="flight-list">
            <!-- Loaded dynamically -->
        </ul>
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
                                <!-- Telemetry rows -->
                            </tbody>
                        </table>
                    </div>
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
        let activeFlightId = null;
        let lastSeenTimestamp = 0;
        let searchQuery = '';

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
                const response = await fetch('/api/flights');
                const data = await response.json();
                
                // Merge new metadata into existing live/recent list
                data.forEach(newFlight => {
                    const idx = flightsData.findIndex(f => f.flight_id === newFlight.flight_id);
                    if (idx !== -1) {
                        // Preserving live updates for lat/lon if they are newer
                        if (isFlightLive(flightsData[idx])) {
                            // keeps live coordinates but updates basic info
                            flightsData[idx].callsign = newFlight.callsign || flightsData[idx].callsign;
                            flightsData[idx].model = newFlight.model || flightsData[idx].model;
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

                // Clean up entries that are old and not live
                const remoteFlightIds = new Set(data.map(f => f.flight_id));
                flightsData = flightsData.filter(f => remoteFlightIds.has(f.flight_id) || isFlightLive(f) || f.flight_id === activeFlightId);

                // Sort flights: live first, then by start time descending
                flightsData.sort((a, b) => {
                    const aLive = isFlightLive(a);
                    const bLive = isFlightLive(b);
                    if (aLive && !bLive) return -1;
                    if (!aLive && bLive) return 1;
                    return b.start_time - a.start_time;
                });

                document.getElementById('flight-count').innerText = data.length;
                renderFlightList();
                plotAllFlights();
            } catch (err) {
                console.error("Failed to fetch flights", err);
            }
        }

        function isFlightLive(flight) {
            const now = Date.now() / 1000;
            return (now - flight.end_time) < 45;
        }

        function renderFlightList() {
            const list = document.getElementById('flight-list');
            list.innerHTML = '';
            
            const filtered = flightsData.filter(flight => {
                const callsign = (flight.callsign || '').toLowerCase();
                const icao = (flight.icao || '').toLowerCase();
                const model = (flight.model || '').toLowerCase();
                return callsign.includes(searchQuery) || icao.includes(searchQuery) || model.includes(searchQuery);
            });
            
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
                        <span class="flight-callsign">${statusDot} ${flight.callsign || 'UNKNOWN'}</span>
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
            const activeFlightIds = new Set(flightsData.map(f => f.flight_id));

            // Clean up markers for flights no longer in local storage and not selected
            Object.keys(planeMarkers).forEach(flightId => {
                if (!activeFlightIds.has(flightId) && flightId !== activeFlightId) {
                    map.removeLayer(planeMarkers[flightId]);
                    delete planeMarkers[flightId];
                }
            });

            // Plot all planes
            flightsData.forEach(flight => {
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
                const response = await fetch(`/api/flight?flight_id=${encodeURIComponent(flightId)}`);
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
                const response = await fetch(`/api/telemetry?flight_id=${encodeURIComponent(flightId)}`);
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

            // Populate telemetry table log
            fetchTelemetryTable(flightDetail.flight_id);
        }

        async function fetchTelemetryTable(flightId) {
            try {
                const response = await fetch(`/api/telemetry?flight_id=${encodeURIComponent(flightId)}`);
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
                                owner: null,
                                country: null,
                                zone: 'Live',
                                level: 'Live',
                                start_time: point.timestamp,
                                end_time: point.timestamp,
                                latitude: point.latitude,
                                longitude: point.longitude,
                                heading: point.heading,
                                altitude: point.altitude,
                                speed: point.speed
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
                            fetch(`/api/flight?flight_id=${encodeURIComponent(flightId)}`)
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
        });

        async function init() {
            await fetchFlights();
            setInterval(pollLiveTelemetry, 2000);
            setInterval(fetchFlights, 10000);
            pollLiveTelemetry();
        }

        init();
    </script>
</body>
</html>
"""
