"""
Mock data store for PyAerial web portal --mock mode.
Provides simulated live and historical flight telemetry, alerts, and details.
"""
from __future__ import annotations

import math
import time
from typing import Any


class MockStore:
    """Simulates Redis and MongoDB stores with realistic generated flight data."""

    def __init__(self, home_lat: float = 35.7275, home_lon: float = -78.6959):
        self.home_lat = home_lat
        self.home_lon = home_lon
        self._start_time = time.time()

        # Initial live flights
        self.live_flights: list[dict[str, Any]] = [
            {
                "flight_id": "mock_live_1",
                "icao": "A1B2C3",
                "callsign": "N123AB",
                "model": "Cessna 172 Skyhawk",
                "owner": "Raleigh Flying Club",
                "country": "United States",
                "aircraft_type": "C172",
                "registration": "N123AB",
                "altitude": 1200.0,
                "speed": 180.0,
                "heading": 45.0,
                "zone": "aerpaw",
                "level": "warn",
                "is_live": True,
                "status": "live",
                "retained": True,
                "start_time": self._start_time - 900,
                "lat_offset": 0.002,
                "lon_offset": 0.001,
                "radius": 0.006,
                "speed_rad": 0.04,
                "phase": 0.0,
            },
            {
                "flight_id": "mock_live_2",
                "icao": "B4C5D6",
                "callsign": "DRONE01",
                "model": "DJI Matrice 300 RTK",
                "owner": "AERPAW Research",
                "country": "United States",
                "aircraft_type": "QUAD",
                "registration": "N999AP",
                "altitude": 120.0,
                "speed": 45.0,
                "heading": 210.0,
                "zone": "aerpaw",
                "level": "alert",
                "is_live": True,
                "status": "live",
                "retained": True,
                "start_time": self._start_time - 450,
                "lat_offset": -0.001,
                "lon_offset": -0.002,
                "radius": 0.003,
                "speed_rad": 0.08,
                "phase": 2.0,
            },
            {
                "flight_id": "mock_live_3",
                "icao": "C7D8E9",
                "callsign": "MEDEVAC1",
                "model": "Eurocopter EC135",
                "owner": "Duke Life Flight",
                "country": "United States",
                "aircraft_type": "EC35",
                "registration": "N456LF",
                "altitude": 450.0,
                "speed": 210.0,
                "heading": 315.0,
                "zone": None,
                "level": None,
                "is_live": True,
                "status": "live",
                "retained": False,
                "start_time": self._start_time - 300,
                "lat_offset": 0.008,
                "lon_offset": -0.005,
                "radius": 0.009,
                "speed_rad": 0.03,
                "phase": 4.0,
            },
        ]

        now = time.time()
        # Historical flights
        self.history_flights: list[dict[str, Any]] = [
            {
                "flight_id": "mock_hist_1",
                "icao": "D9E0F1",
                "callsign": "PIPER88",
                "model": "Piper PA-28 Cherokee",
                "owner": "Private Owner",
                "country": "United States",
                "aircraft_type": "P28A",
                "registration": "N888PA",
                "altitude": 950.0,
                "speed": 165.0,
                "heading": 180.0,
                "zone": "aerpaw",
                "level": "warn",
                "is_live": False,
                "status": "completed",
                "retained": True,
                "start_time": now - 7200,
                "end_time": now - 3600,
                "timestamp": now - 3600,
                "latitude": home_lat + 0.003,
                "longitude": home_lon - 0.004,
            },
            {
                "flight_id": "mock_hist_2",
                "icao": "E1F2A3",
                "callsign": "SCANNER2",
                "model": "Beechcraft King Air B200",
                "owner": "State Highway Patrol",
                "country": "United States",
                "aircraft_type": "BE20",
                "registration": "N200HP",
                "altitude": 2800.0,
                "speed": 310.0,
                "heading": 90.0,
                "zone": None,
                "level": None,
                "is_live": False,
                "status": "completed",
                "retained": True,
                "start_time": now - 14400,
                "end_time": now - 10800,
                "timestamp": now - 10800,
                "latitude": home_lat - 0.012,
                "longitude": home_lon + 0.015,
            },
            {
                "flight_id": "mock_hist_3",
                "icao": "F3A4B5",
                "callsign": "SURVEY05",
                "model": "Cessna 208B Grand Caravan",
                "owner": "Aerial Mapping Corp",
                "country": "United States",
                "aircraft_type": "C208",
                "registration": "N505AM",
                "altitude": 1600.0,
                "speed": 220.0,
                "heading": 270.0,
                "zone": "aerpaw",
                "level": "alert",
                "is_live": False,
                "status": "completed",
                "retained": True,
                "start_time": now - 21600,
                "end_time": now - 18000,
                "timestamp": now - 18000,
                "latitude": home_lat + 0.001,
                "longitude": home_lon + 0.002,
            },
        ]

        self.telemetry: dict[str, list[dict[str, Any]]] = {}
        self.live_telemetry_stream: list[dict[str, Any]] = []

        self._seed_telemetry()
        self._seed_alerts()

    def _seed_telemetry(self) -> None:
        now = time.time()
        for flight in self.history_flights:
            fid = flight["flight_id"]
            points = []
            start_t = flight["start_time"]
            end_t = flight["end_time"]
            step = (end_t - start_t) / 30.0
            base_lat = flight["latitude"]
            base_lon = flight["longitude"]
            for i in range(31):
                t = start_t + i * step
                lat = base_lat + (math.sin(i * 0.2) * 0.008)
                lon = base_lon + (math.cos(i * 0.2) * 0.008)
                points.append({
                    "flight_id": fid,
                    "icao": flight["icao"],
                    "timestamp": t,
                    "latitude": lat,
                    "longitude": lon,
                    "altitude": flight["altitude"] + (i * 5.0),
                    "speed": flight["speed"],
                    "heading": (flight["heading"] + i * 2) % 360,
                    "zone": flight.get("zone"),
                    "level": flight.get("level"),
                })
            self.telemetry[fid] = points

        for flight in self.live_flights:
            fid = flight["flight_id"]
            points = []
            start_t = flight["start_time"]
            step = 10.0
            steps = max(1, int((now - start_t) / step))
            phase = flight["phase"] - (steps * flight["speed_rad"])
            for i in range(steps + 1):
                t = start_t + i * step
                p = phase + i * flight["speed_rad"]
                lat = self.home_lat + flight["lat_offset"] + math.sin(p) * flight["radius"]
                lon = self.home_lon + flight["lon_offset"] + math.cos(p) * flight["radius"]
                heading = (math.degrees(math.atan2(math.cos(p), -math.sin(p))) + 360) % 360
                points.append({
                    "flight_id": fid,
                    "icao": flight["icao"],
                    "timestamp": t,
                    "latitude": lat,
                    "longitude": lon,
                    "altitude": flight["altitude"],
                    "speed": flight["speed"],
                    "heading": round(heading, 1),
                    "zone": flight.get("zone"),
                    "level": flight.get("level"),
                })
            self.telemetry[fid] = points
            if points:
                flight["latitude"] = points[-1]["latitude"]
                flight["longitude"] = points[-1]["longitude"]
                flight["heading"] = points[-1]["heading"]
                flight["timestamp"] = points[-1]["timestamp"]

    def _seed_alerts(self) -> None:
        now = time.time()
        self.live_alerts: list[dict[str, Any]] = [
            {
                "alert_id": "alert_mock_1",
                "flight_id": "mock_live_1",
                "icao": "A1B2C3",
                "callsign": "N123AB",
                "zone": "aerpaw",
                "level": "warn",
                "timestamp": now - 300,
                "eta": 45.0,
                "altitude": 1200.0,
                "latitude": self.home_lat + 0.002,
                "longitude": self.home_lon + 0.001,
            },
            {
                "alert_id": "alert_mock_2",
                "flight_id": "mock_live_2",
                "icao": "B4C5D6",
                "callsign": "DRONE01",
                "zone": "aerpaw",
                "level": "alert",
                "timestamp": now - 120,
                "eta": 15.0,
                "altitude": 120.0,
                "latitude": self.home_lat - 0.001,
                "longitude": self.home_lon - 0.002,
            },
        ]

        self.history_alerts: list[dict[str, Any]] = [
            {
                "alert_id": "alert_hist_1",
                "flight_id": "mock_hist_1",
                "icao": "D9E0F1",
                "callsign": "PIPER88",
                "zone": "aerpaw",
                "level": "warn",
                "timestamp": now - 5400,
                "eta": 30.0,
                "altitude": 950.0,
                "latitude": self.home_lat + 0.003,
                "longitude": self.home_lon - 0.004,
            },
            {
                "alert_id": "alert_hist_2",
                "flight_id": "mock_hist_3",
                "icao": "F3A4B5",
                "callsign": "SURVEY05",
                "zone": "aerpaw",
                "level": "alert",
                "timestamp": now - 19800,
                "eta": 10.0,
                "altitude": 1600.0,
                "latitude": self.home_lat + 0.001,
                "longitude": self.home_lon + 0.002,
            },
        ]

    def update_live(self) -> list[dict[str, Any]]:
        """Advance live flight positions and return new telemetry points."""
        now = time.time()
        new_points = []
        for flight in self.live_flights:
            flight["phase"] += flight["speed_rad"]
            p = flight["phase"]
            lat = self.home_lat + flight["lat_offset"] + math.sin(p) * flight["radius"]
            lon = self.home_lon + flight["lon_offset"] + math.cos(p) * flight["radius"]
            heading = (math.degrees(math.atan2(math.cos(p), -math.sin(p))) + 360) % 360

            flight["latitude"] = lat
            flight["longitude"] = lon
            flight["heading"] = round(heading, 1)
            flight["timestamp"] = now

            point = {
                "flight_id": flight["flight_id"],
                "icao": flight["icao"],
                "timestamp": now,
                "latitude": lat,
                "longitude": lon,
                "altitude": flight["altitude"],
                "speed": flight["speed"],
                "heading": flight["heading"],
                "zone": flight.get("zone"),
                "level": flight.get("level"),
            }
            if flight["flight_id"] not in self.telemetry:
                self.telemetry[flight["flight_id"]] = []
            self.telemetry[flight["flight_id"]].append(point)
            new_points.append(point)
            self.live_telemetry_stream.append(point)
        return new_points

    def get_live_flights(self) -> list[dict[str, Any]]:
        return [dict(f) for f in self.live_flights]

    def get_history_flights(self) -> list[dict[str, Any]]:
        return [dict(f) for f in self.history_flights]

    def get_flight_detail(self, flight_id: str, view: str) -> dict[str, Any] | None:
        pool = self.live_flights if view == "live" else self.history_flights
        for flight in pool:
            if flight["flight_id"] == flight_id:
                res = dict(flight)
                res["registration"] = res.get("registration") or "N/A"
                res["photo_url"] = None
                res["photo_photographer"] = None
                res["photo_link"] = None
                return res
        for flight in (self.history_flights if view == "live" else self.live_flights):
            if flight["flight_id"] == flight_id:
                res = dict(flight)
                res["registration"] = res.get("registration") or "N/A"
                res["photo_url"] = None
                res["photo_photographer"] = None
                res["photo_link"] = None
                return res
        return None

    def get_telemetry(self, flight_id: str, since: float = 0.0) -> list[dict[str, Any]]:
        points = self.telemetry.get(flight_id, [])
        if since > 0:
            points = [p for p in points if p["timestamp"] > since]
        return points

    def get_live_telemetry(self, since: float = 0.0) -> list[dict[str, Any]]:
        if since > 0:
            return [p for p in self.live_telemetry_stream if p["timestamp"] > since]
        return self.live_telemetry_stream[-10:] if self.live_telemetry_stream else []

    def get_alerts(
        self,
        view: str,
        since: float = 0.0,
        flight_id: str | None = None,
        level: str | None = None,
        limit: int = 0,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        alerts = list(self.live_alerts if view == "live" else self.history_alerts)
        if since > 0:
            alerts = [a for a in alerts if a.get("timestamp", 0) > since]
        if flight_id:
            alerts = [a for a in alerts if a.get("flight_id") == flight_id]
        if level:
            alerts = [a for a in alerts if (a.get("level") or "").lower() == level.lower()]
        if skip:
            alerts = alerts[skip:]
        if limit > 0:
            alerts = alerts[:limit]
        return alerts
