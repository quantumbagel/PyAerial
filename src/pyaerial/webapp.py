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
                })
            self.send_json(flights)
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
    <style>
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }
        body {
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
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
            color: #777;
        }
        #stats-panel {
            padding: 12px 20px;
            background-color: #151515;
            border-bottom: 1px solid #2d2d2d;
            font-size: 0.8rem;
            display: flex;
            justify-content: space-between;
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
            margin-bottom: 3px;
        }
        .flight-callsign {
            font-weight: 600;
            font-size: 0.9rem;
            color: #fff;
        }
        .flight-icao {
            font-family: monospace;
            background-color: #2d2d2d;
            padding: 1px 4px;
            border-radius: 3px;
            font-size: 0.7rem;
            color: #ccc;
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
        #details-overlay {
            position: absolute;
            bottom: 20px;
            left: 20px;
            z-index: 1000;
            background-color: rgba(22, 22, 22, 0.95);
            border: 1px solid #2d2d2d;
            border-radius: 6px;
            padding: 12px;
            width: 280px;
            display: none;
        }
        #details-overlay h2 {
            font-size: 0.95rem;
            color: #3b82f6;
            margin-bottom: 8px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .details-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 6px;
            font-size: 0.75rem;
        }
        .details-label {
            color: #777;
        }
        .details-value {
            color: #fff;
            text-align: right;
            font-weight: 500;
        }
        .plane-icon-div {
            background: none;
            border: none;
        }
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
        ::-webkit-scrollbar-thumb:hover {
            background: #444;
        }
    </style>
</head>
<body>
    <div id="sidebar">
        <div id="sidebar-header">
            <h1>PyAerial Live Tracker</h1>
            <p>ADS-B Geofence Scanner Console</p>
        </div>
        <div id="stats-panel">
            <span>Recent Flights: <strong id="flight-count">0</strong></span>
            <span>Live Map: <strong id="live-count" style="color: #10b981;">0</strong></span>
        </div>
        <ul id="flight-list">
            <!-- Loaded dynamically -->
        </ul>
    </div>
    <div id="map-container">
        <div id="map"></div>
        <div id="details-overlay">
            <h2><span id="detail-callsign">N/A</span> <span id="detail-icao" class="flight-icao">N/A</span></h2>
            <div class="details-grid">
                <span class="details-label">Model:</span>
                <span class="details-value" id="detail-model">N/A</span>
                <span class="details-label">Owner:</span>
                <span class="details-value" id="detail-owner">N/A</span>
                <span class="details-label">Altitude:</span>
                <span class="details-value" id="detail-altitude">N/A</span>
                <span class="details-label">Speed:</span>
                <span class="details-value" id="detail-speed">N/A</span>
                <span class="details-label">Heading:</span>
                <span class="details-value" id="detail-heading">N/A</span>
                <span class="details-label">Zone / Level:</span>
                <span class="details-value" id="detail-zone-level" style="color: #f59e0b;">N/A</span>
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

        function createPlaneIcon(heading, isSelected) {
            const fill = isSelected ? '#ef4444' : '#3b82f6';
            const stroke = isSelected ? '#7f1d1d' : '#1e3a8a';
            const size = isSelected ? 28 : 22;
            
            const svg = `
                <svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" style="transform: rotate(${heading || 0}deg); transform-origin: center;">
                    <path d="M12 2L22 22L12 18L2 22L12 2Z" fill="${fill}" stroke="${stroke}" stroke-width="1.5" stroke-linejoin="round"/>
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
                flightsData = data;
                document.getElementById('flight-count').innerText = data.length;
                renderFlightList();
            } catch (err) {
                console.error("Failed to fetch flights", err);
            }
        }

        function renderFlightList() {
            const list = document.getElementById('flight-list');
            list.innerHTML = '';
            flightsData.forEach(flight => {
                const item = document.createElement('li');
                item.className = 'flight-item';
                if (flight.flight_id === activeFlightId) {
                    item.className += ' active';
                }
                
                const startTime = new Date(flight.start_time * 1000).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
                
                item.innerHTML = `
                    <div class="flight-meta-row">
                        <span class="flight-callsign">${flight.callsign || 'UNKNOWN'}</span>
                        <span class="flight-icao">${flight.icao.toUpperCase()}</span>
                    </div>
                    <div class="flight-meta-row">
                        <span class="flight-desc">${flight.model || 'Unknown Model'}</span>
                        <span class="flight-time">${startTime}</span>
                    </div>
                `;
                item.addEventListener('click', () => selectFlight(flight.flight_id));
                list.appendChild(item);
            });
        }

        async function selectFlight(flightId) {
            activeFlightId = flightId;
            renderFlightList();
            
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
                        const path = L.polyline(latlngs, {color: '#ef4444', weight: 2, opacity: 0.85}).addTo(map);
                        planePaths[flightId] = path;
                        
                        const lastPoint = points[points.length - 1];
                        map.setView([lastPoint.latitude, lastPoint.longitude]);
                        showDetails(flightId, lastPoint);
                    }
                }
            } catch (err) {
                console.error("Failed to fetch flight telemetry", err);
            }
        }

        function showDetails(flightId, lastTelemetry) {
            const flight = flightsData.find(f => f.flight_id === flightId);
            if (!flight) return;

            document.getElementById('detail-callsign').innerText = flight.callsign || 'UNKNOWN';
            document.getElementById('detail-icao').innerText = flight.icao.toUpperCase();
            document.getElementById('detail-model').innerText = flight.model || 'Unknown';
            document.getElementById('detail-owner').innerText = flight.owner || 'Unknown';
            document.getElementById('detail-altitude').innerText = lastTelemetry && lastTelemetry.altitude !== null ? `${Math.round(lastTelemetry.altitude)} m` : 'N/A';
            document.getElementById('detail-speed').innerText = lastTelemetry && lastTelemetry.speed !== null ? `${Math.round(lastTelemetry.speed * 1.94384)} kt` : 'N/A';
            document.getElementById('detail-heading').innerText = lastTelemetry && lastTelemetry.heading !== null ? `${Math.round(lastTelemetry.heading)}°` : 'N/A';
            document.getElementById('detail-zone-level').innerText = `${flight.zone} / ${flight.level}`;
            
            document.getElementById('details-overlay').style.display = 'block';
        }

        async function pollLiveTelemetry() {
            try {
                const response = await fetch(`/api/live?since=${lastSeenTimestamp}`);
                const data = await response.json();
                lastSeenTimestamp = data.timestamp;

                const points = data.telemetry;
                const latestByFlight = {};
                points.forEach(point => {
                    if (point.latitude !== undefined && point.longitude !== undefined) {
                        latestByFlight[point.flight_id] = point;
                    }
                });

                Object.keys(latestByFlight).forEach(flightId => {
                    const point = latestByFlight[flightId];
                    const pos = [point.latitude, point.longitude];

                    const isSelected = flightId === activeFlightId;

                    if (planeMarkers[flightId]) {
                        planeMarkers[flightId].setLatLng(pos);
                        planeMarkers[flightId].setIcon(createPlaneIcon(point.heading, isSelected));
                    } else {
                        const marker = L.marker(pos, {
                            icon: createPlaneIcon(point.heading, isSelected)
                        }).addTo(map);
                        
                        marker.on('click', () => selectFlight(flightId));
                        planeMarkers[flightId] = marker;
                    }

                    if (isSelected && planePaths[flightId]) {
                        planePaths[flightId].addLatLng(pos);
                        showDetails(flightId, point);
                    }
                });

                document.getElementById('live-count').innerText = Object.keys(planeMarkers).length;
            } catch (err) {
                console.error("Failed to poll live telemetry", err);
            }
        }

        async function init() {
            await fetchFlights();
            setInterval(pollLiveTelemetry, 2000);
            setInterval(fetchFlights, 15000);
            pollLiveTelemetry();
        }

        init();
    </script>
</body>
</html>
"""
