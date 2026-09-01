"""
Mock live store for ``pyaerial live --mock`` / ``pyaerial view --mock``.

Web ``--mock`` uses the real engine with a mock receiver instead of this class.
"""

from __future__ import annotations

import math
import time
from typing import Any


def _active_alert(
    alert_id: str,
    zone: str,
    rule: str,
    activated_at: float,
    eta: float | None = None,
    mock_lifetime: float = 120.0,
) -> dict[str, Any]:
    return {
        "alert_id": alert_id,
        "zone": zone,
        "rule": rule,
        "activated_at": activated_at,
        "eta": eta,
        "mock_lifetime": mock_lifetime,
    }


class MockStore:
    """In-memory simulated live flights for the terminal viewer."""

    def __init__(
        self,
        home_lat: float = 35.7275,
        home_lon: float = -78.6959,
    ):
        self.home_lat = home_lat
        self.home_lon = home_lon
        self._start_time = time.time()

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
                "active_alerts": [
                    _active_alert(
                        "mock_live_1:aerpaw:warn",
                        "aerpaw",
                        "warn",
                        self._start_time - 30,
                        45.0,
                        90.0,
                    )
                ],
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
                "active_alerts": [
                    _active_alert(
                        "mock_live_2:aerpaw:alert",
                        "aerpaw",
                        "alert",
                        self._start_time - 20,
                        15.0,
                        75.0,
                    )
                ],
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
                "active_alerts": [
                    _active_alert(
                        "mock_live_3:cool:cool",
                        "cool",
                        "cool",
                        self._start_time - 45,
                        60.0,
                        120.0,
                    )
                ],
                "is_live": True,
                "status": "live",
                "retained": True,
                "start_time": self._start_time - 300,
                "lat_offset": 0.008,
                "lon_offset": -0.005,
                "radius": 0.009,
                "speed_rad": 0.03,
                "phase": 4.0,
            },
        ]

        self.telemetry: dict[str, list[dict[str, Any]]] = {}
        self._seed_telemetry()
        self._seed_alerts()

    def _seed_telemetry(self) -> None:
        now = time.time()
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
                lat = (
                    self.home_lat
                    + flight["lat_offset"]
                    + math.sin(p) * flight["radius"]
                )
                lon = (
                    self.home_lon
                    + flight["lon_offset"]
                    + math.cos(p) * flight["radius"]
                )
                heading = (
                    math.degrees(math.atan2(math.cos(p), -math.sin(p))) + 360
                ) % 360
                points.append(
                    {
                        "flight_id": fid,
                        "icao": flight["icao"],
                        "timestamp": t,
                        "latitude": lat,
                        "longitude": lon,
                        "altitude": flight["altitude"],
                        "speed": flight["speed"],
                        "heading": round(heading, 1),
                        "active_alerts": flight.get("active_alerts", []),
                    }
                )
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
                "alert_id": "mock_live_1:aerpaw:warn",
                "flight_id": "mock_live_1",
                "icao": "A1B2C3",
                "callsign": "N123AB",
                "zone": "aerpaw",
                "rule": "warn",
                "active": True,
                "activated_at": now - 30,
                "deactivated_at": None,
                "mock_lifetime": 90.0,
                "eta": 45.0,
                "altitude": 1200.0,
                "latitude": self.home_lat + 0.002,
                "longitude": self.home_lon + 0.001,
            },
            {
                "alert_id": "mock_live_2:aerpaw:alert",
                "flight_id": "mock_live_2",
                "icao": "B4C5D6",
                "callsign": "DRONE01",
                "zone": "aerpaw",
                "rule": "alert",
                "active": True,
                "activated_at": now - 20,
                "deactivated_at": None,
                "mock_lifetime": 75.0,
                "eta": 15.0,
                "altitude": 120.0,
                "latitude": self.home_lat - 0.001,
                "longitude": self.home_lon - 0.002,
            },
            {
                "alert_id": "mock_live_3:cool:cool",
                "flight_id": "mock_live_3",
                "icao": "C7D8E9",
                "callsign": "MEDEVAC1",
                "zone": "cool",
                "rule": "cool",
                "active": True,
                "activated_at": now - 45,
                "deactivated_at": None,
                "mock_lifetime": 120.0,
                "eta": 60.0,
                "altitude": 450.0,
                "latitude": self.home_lat + 0.008,
                "longitude": self.home_lon - 0.005,
            },
        ]

    def _deactivate_alert(self, alert: dict[str, Any], now: float) -> None:
        alert_id = alert.get("alert_id")
        if not alert_id:
            return
        alert["active"] = False
        alert["deactivated_at"] = now
        flight_id = alert.get("flight_id")
        for flight in self.live_flights:
            if flight.get("flight_id") != flight_id:
                continue
            flight["active_alerts"] = [
                item
                for item in (flight.get("active_alerts") or [])
                if item.get("alert_id") != alert_id
            ]
            break

    def update_live(self) -> list[dict[str, Any]]:
        now = time.time()
        for alert in self.live_alerts:
            if not alert.get("active", True):
                continue
            activated_at = alert.get("activated_at") or now
            lifetime = float(alert.get("mock_lifetime") or 120.0)
            if now - activated_at >= lifetime:
                self._deactivate_alert(alert, now)

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
                "active_alerts": flight.get("active_alerts", []),
            }
            self.telemetry.setdefault(flight["flight_id"], []).append(point)
            new_points.append(point)
        return new_points

    def get_flights(self) -> list[dict[str, Any]]:
        now = time.time()
        results = []
        for flight in self.live_flights:
            row = dict(flight)
            active = row.get("active_alerts") or []
            if active:
                row["alert_stats"] = {
                    "episode_count": len(active),
                    "total_seconds": int(
                        sum(
                            max(0.0, now - (a.get("activated_at") or now))
                            for a in active
                        )
                    ),
                    "active_count": len(active),
                }
            results.append(row)
        return results

    def get_telemetry(self, flight_id: str, since: float = 0.0) -> list[dict[str, Any]]:
        points = self.telemetry.get(flight_id, [])
        if since > 0:
            points = [p for p in points if p["timestamp"] > since]
        return points
