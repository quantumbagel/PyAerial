"""
Mock data store for PyAerial web portal --mock mode.
Provides simulated live and historical flight telemetry, alerts, and details.
"""
from __future__ import annotations

import math
import time
from typing import Any

from pyaerial.calc.projection import _sample_intent_path, _sample_track_path


def _active_alert(alert_id: str, zone: str, rule: str, activated_at: float,
                  eta: float | None = None, mock_lifetime: float = 120.0) -> dict[str, Any]:
    return {
        "alert_id": alert_id,
        "zone": zone,
        "rule": rule,
        "activated_at": activated_at,
        "mock_lifetime": mock_lifetime,
    }


_MOCK_PHOTOS: dict[str, dict[str, str]] = {
    "A1B2C3": {
        "photo_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ae/Cessna_172S_Skyhawk_SP%2C_Private_JP6817971.jpg/640px-Cessna_172S_Skyhawk_SP%2C_Private_JP6817971.jpg",
        "photo_photographer": "Wikimedia Commons / Julian Herzog",
        "photo_link": "https://commons.wikimedia.org/wiki/File:Cessna_172S_Skyhawk_SP,_Private_JP6817971.jpg",
    },
    "B4C5D6": {
        "photo_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1a/DJI_Matrice_300_RTK_in_flight.jpg/640px-DJI_Matrice_300_RTK_in_flight.jpg",
        "photo_photographer": "Wikimedia Commons",
        "photo_link": "https://commons.wikimedia.org/wiki/File:DJI_Matrice_300_RTK_in_flight.jpg",
    },
    "C7D8E9": {
        "photo_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/d4/ADAC_Air_Rescuers_Eurocopter_EC135.jpg/640px-ADAC_Air_Rescuers_Eurocopter_EC135.jpg",
        "photo_photographer": "Wikimedia Commons / Airwolf",
        "photo_link": "https://commons.wikimedia.org/wiki/File:ADAC_Air_Rescuers_Eurocopter_EC135.jpg",
    },
    "D9E0F1": {
        "photo_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/87/Piper_PA-28-181_Archer_II_N3029R_01.jpg/640px-Piper_PA-28-181_Archer_II_N3029R_01.jpg",
        "photo_photographer": "Wikimedia Commons",
        "photo_link": "https://commons.wikimedia.org/wiki/File:Piper_PA-28-181_Archer_II_N3029R_01.jpg",
    },
    "A835AF": {
        "photo_url": "https://t.plnspttrs.net/25966/1577891_37fc82da9a_280.jpg",
        "photo_photographer": "Demo Borstell",
        "photo_link": "https://www.planespotters.net/photo/1577891/n628ts-spacex-gulfstream-g650er-gvi?utm_source=api",
    },
}


def _enrich_mock_photo(res: dict[str, Any], aircraft_db: Any) -> None:
    icao = (res.get("icao") or "").upper()
    if aircraft_db:
        try:
            meta = aircraft_db.lookup_cached(icao)
            if meta and meta.get("photo_url"):
                res["photo_url"] = meta.get("photo_url")
                res["photo_photographer"] = meta.get("photo_photographer")
                res["photo_link"] = meta.get("photo_link")
                return
        except Exception:
            pass

    fallback = _MOCK_PHOTOS.get(icao) or _MOCK_PHOTOS.get((res.get("icao") or "").lower())
    if fallback:
        res["photo_url"] = fallback.get("photo_url")
        res["photo_photographer"] = fallback.get("photo_photographer")
        res["photo_link"] = fallback.get("photo_link")
    else:
        res["photo_url"] = None
        res["photo_photographer"] = None
        res["photo_link"] = None


class MockStore:
    """Simulates Redis and MongoDB stores with realistic generated flight data."""

    def __init__(self, home_lat: float = 35.7275, home_lon: float = -78.6959, simulated_delay: float = 0.0,
                 aircraft_db: Any = None):
        self.home_lat = home_lat
        self.home_lon = home_lon
        self.simulated_delay = simulated_delay
        self.aircraft_db = aircraft_db
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
                "active_alerts": [_active_alert("mock_live_1:aerpaw:warn", "aerpaw", "warn", self._start_time - 30, 45.0, 90.0)],
                "is_live": True,
                "status": "live",
                "retained": True,
                "start_time": self._start_time - 900,
                "lat_offset": 0.002,
                "lon_offset": 0.001,
                "radius": 0.006,
                "speed_rad": 0.04,
                "phase": 0.0,
                "mock_selected_heading": 35.0,
                "mock_selected_altitude": 1500.0,
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
                "active_alerts": [_active_alert("mock_live_2:aerpaw:alert", "aerpaw", "alert", self._start_time - 20, 15.0, 75.0)],
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
                "active_alerts": [_active_alert("mock_live_3:cool:cool", "cool", "cool", self._start_time - 45, 60.0, 120.0)],
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

        now = time.time()
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
                "active_alerts": [],
                "is_live": False,
                "status": "completed",
                "retained": True,
                "start_time": now - 7200,
                "end_time": now - 3600,
                "timestamp": now - 3600,
                "alert_stats": {"episode_count": 1, "total_seconds": 1600, "active_count": 0},
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
                "active_alerts": [],
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
                "active_alerts": [],
                "is_live": False,
                "status": "completed",
                "retained": True,
                "start_time": now - 21600,
                "end_time": now - 18000,
                "timestamp": now - 18000,
                "alert_stats": {"episode_count": 1, "total_seconds": 1000, "active_count": 0},
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
                    "active_alerts": flight.get("active_alerts", []),
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
                    "active_alerts": flight.get("active_alerts", []),
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

        self.history_alerts: list[dict[str, Any]] = [
            {
                "alert_id": "mock_hist_1:aerpaw:warn",
                "flight_id": "mock_hist_1",
                "icao": "D9E0F1",
                "callsign": "PIPER88",
                "zone": "aerpaw",
                "rule": "warn",
                "active": False,
                "activated_at": now - 5600,
                "deactivated_at": now - 4000,
                "eta": 30.0,
                "altitude": 950.0,
                "latitude": self.home_lat + 0.003,
                "longitude": self.home_lon - 0.004,
            },
            {
                "alert_id": "mock_hist_3:aerpaw:alert",
                "flight_id": "mock_hist_3",
                "icao": "F3A4B5",
                "callsign": "SURVEY05",
                "zone": "aerpaw",
                "rule": "alert",
                "active": False,
                "activated_at": now - 20000,
                "deactivated_at": now - 19000,
                "eta": 10.0,
                "altitude": 1600.0,
                "latitude": self.home_lat + 0.001,
                "longitude": self.home_lon + 0.002,
            },
            {
                "alert_id": "mock_hist_2:cool:cool",
                "flight_id": "mock_hist_2",
                "icao": "E1F2A3",
                "callsign": "SCANNER2",
                "zone": "cool",
                "rule": "cool",
                "active": False,
                "activated_at": now - 12000,
                "deactivated_at": now - 11000,
                "eta": 45.0,
                "altitude": 2800.0,
                "latitude": self.home_lat - 0.012,
                "longitude": self.home_lon + 0.015,
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
                item for item in (flight.get("active_alerts") or [])
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

            position = (lat, lon)
            speed = float(flight["speed"])
            track_path = _sample_track_path(
                position, heading, speed, 0.0,
                horizon_seconds=120, step_seconds=2, curved=False,
            )
            selected_heading = flight.get("mock_selected_heading")
            if selected_heading is not None:
                selected_heading = (heading + float(selected_heading)) % 360.0
            intent_path = None
            if selected_heading is not None:
                intent_path = _sample_intent_path(
                    position, heading, speed, 0.0, selected_heading,
                    horizon_seconds=120, step_seconds=2,
                )
            flight["portal_projection"] = {
                "horizon_seconds": 120,
                "step_seconds": 2,
                "track_path": track_path,
                "intent_path": intent_path,
                "selected_heading": selected_heading,
                "selected_altitude": flight.get("mock_selected_altitude"),
                "motion_heading": heading,
                "motion_speed_kph": speed,
                "turn_rate_deg_s": 0.0,
            }

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
            if flight["flight_id"] not in self.telemetry:
                self.telemetry[flight["flight_id"]] = []
            self.telemetry[flight["flight_id"]].append(point)
            new_points.append(point)
            self.live_telemetry_stream.append(point)
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
                    "total_seconds": int(sum(max(0.0, now - (a.get("activated_at") or now)) for a in active)),
                    "active_count": len(active),
                }
            results.append(row)
        return results

    def get_history_flights(self) -> list[dict[str, Any]]:
        return [dict(f) for f in self.history_flights]

    def get_flight_detail(self, flight_id: str, view: str) -> dict[str, Any] | None:
        pool = self.live_flights if view == "live" else self.history_flights
        for flight in pool:
            if flight["flight_id"] == flight_id:
                res = dict(flight)
                res["registration"] = res.get("registration") or "N/A"
                _enrich_mock_photo(res, self.aircraft_db)
                return res
        for flight in (self.history_flights if view == "live" else self.live_flights):
            if flight["flight_id"] == flight_id:
                res = dict(flight)
                res["registration"] = res.get("registration") or "N/A"
                _enrich_mock_photo(res, self.aircraft_db)
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
        rule: str | None = None,
        limit: int = 0,
        skip: int = 0,
        active_only: bool | None = None,
    ) -> list[dict[str, Any]]:
        if view == "live":
            alerts = list(self.live_alerts) if not flight_id else [
                a for a in self.live_alerts if a.get("flight_id") == flight_id
            ]
            if active_only is not False and not flight_id:
                alerts = [a for a in alerts if a.get("active", True)]
        else:
            alerts = list(self.history_alerts) if not flight_id else [
                a for a in self.history_alerts if a.get("flight_id") == flight_id
            ]
        if since > 0:
            alerts = [a for a in alerts if (a.get("activated_at") or 0) > since]
        if flight_id:
            alerts = [a for a in alerts if a.get("flight_id") == flight_id]
        if rule:
            alerts = [a for a in alerts if (a.get("rule") or "").lower() == rule.lower()]
        alerts.sort(key=lambda a: a.get("activated_at") or 0, reverse=True)
        if skip:
            alerts = alerts[skip:]
        if limit > 0:
            alerts = alerts[:limit]
        return alerts

    def get_stats(self) -> dict[str, int]:
        live_flights = len(self.get_flights())
        active_alerts = len(self.get_alerts("live", active_only=True))
        retained_flights = len(self.history_flights)
        historical_alerts = len(self.history_alerts)
        return {
            "live_flights": live_flights,
            "active_alerts": active_alerts,
            "retained_flights": retained_flights,
            "historical_alerts": historical_alerts,
        }

