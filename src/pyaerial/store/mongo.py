"""MongoDB persistence for retained historical flights, telemetry, and alerts."""
from __future__ import annotations

import logging
import math
import time
from typing import Any

import pymongo
from pymongo.errors import PyMongoError
from shapely import Polygon

from pyaerial.calc import evaluate, geo
from pyaerial.config.schema import Config
from pyaerial.constants import (
    STORE_CALC_DATA,
    STORE_CALLSIGN,
    STORE_FIRST_PACKET,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_MOST_RECENT_PACKET,
    STORE_RECV_DATA,
    STORE_ALT,
)
from pyaerial.models import get_latest

log = logging.getLogger("pyaerial.store")

_RECONNECT_DELAY = 2.0
_ETA_HORIZON = 100_000
_FLIGHT_STATUS_COMPLETED = "completed"


def flight_id_for_plane(plane: dict) -> str:
    icao = plane[STORE_INFO][STORE_ICAO].lower()
    first_packet = plane[STORE_INTERNAL][STORE_FIRST_PACKET]
    return f"{icao}-{int(first_packet)}"


def build_telemetry_docs(plane: dict, flight_id: str, icao: str) -> list[dict[str, Any]]:
    """Build telemetry documents from a plane's in-memory time series."""
    lat_series = plane.get(STORE_RECV_DATA, {}).get(STORE_LAT, [])
    if not lat_series:
        return []

    docs: list[dict[str, Any]] = []
    for lat_datum in lat_series:
        lon_datum = get_latest(STORE_RECV_DATA, STORE_LONG, plane, lat_datum.time)
        if lon_datum is None:
            continue
        alt_datum = get_latest(STORE_RECV_DATA, STORE_ALT, plane, lat_datum.time)
        speed_datum = (
            get_latest(STORE_CALC_DATA, STORE_HORIZ_SPEED, plane, lat_datum.time)
            or get_latest(STORE_RECV_DATA, STORE_HORIZ_SPEED, plane, lat_datum.time)
        )
        heading_datum = (
            get_latest(STORE_CALC_DATA, STORE_HEADING, plane, lat_datum.time)
            or get_latest(STORE_RECV_DATA, STORE_HEADING, plane, lat_datum.time)
        )
        doc: dict[str, Any] = {
            "flight_id": flight_id,
            "icao": icao,
            "timestamp": lat_datum.time,
            "latitude": lat_datum.value,
            "longitude": lon_datum.value,
            "position": {
                "type": "Point",
                "coordinates": [lon_datum.value, lat_datum.value],
            },
        }
        if alt_datum is not None:
            doc["altitude"] = alt_datum.value
        if speed_datum is not None:
            doc["speed"] = speed_datum.value
        if heading_datum is not None:
            doc["heading"] = heading_datum.value
        docs.append(doc)
    return docs


class MongoStore:
    """MongoDB writer for retained completed flights only."""

    def __init__(self, config: Config, polygons: dict[str, Polygon]):
        self.config = config
        self.polygons = polygons
        self.uri = config.database.uri
        self.client: pymongo.MongoClient | None = None
        self.db: pymongo.database.Database | None = None
        self._connect()

    def _connect(self) -> None:
        try:
            self.client = pymongo.MongoClient(
                self.uri,
                serverSelectionTimeoutMS=500,
                connectTimeoutMS=500,
                socketTimeoutMS=500,
            )
            self.client.admin.command("ping")
            if self.config.database.name:
                self.db = self.client.get_database(self.config.database.name)
            else:
                try:
                    self.db = self.client.get_default_database()
                except Exception:
                    self.db = self.client.get_database("pyaerial")
            self._ensure_indexes()
            log.info("Connected to MongoDB database '%s' at %s", self.db.name, self.uri)
        except (PyMongoError, Exception) as exc:
            log.info("MongoDB unavailable at %s; operating in offline mode.", self.uri)
            self.client = None
            self.db = None

    def _ensure_indexes(self) -> None:
        if self.db is None:
            return
        flights = self.db.get_collection("flights")
        flights.create_index([("icao", pymongo.ASCENDING)])
        flights.create_index([("status", pymongo.ASCENDING)])
        flights.create_index([("end_time", pymongo.DESCENDING)])
        flights.create_index([("retained", pymongo.ASCENDING)])

        telemetry = self.db.get_collection("telemetry")
        telemetry.create_index([("flight_id", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)])
        telemetry.create_index([("icao", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)])
        telemetry.create_index([("position", pymongo.GEOSPHERE)])

        alerts = self.db.get_collection("alerts")
        alerts.create_index([("timestamp", pymongo.DESCENDING)])
        alerts.create_index([("flight_id", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)])
        alerts.create_index([("icao", pymongo.ASCENDING), ("timestamp", pymongo.DESCENDING)])

    def _ensure_connected(self) -> bool:
        if self.client is None or self.db is None:
            return False
        try:
            self.client.admin.command("ping")
            return True
        except (PyMongoError, AttributeError):
            log.info("Lost MongoDB connection; operating in offline mode.")
            self.client = None
            self.db = None
            return False

    def finalize_plane(self, plane: dict, *, alerts: list[dict[str, Any]] | None = None) -> None:
        """Persist a completed flight to Mongo if retention rules are met."""
        if not self._ensure_connected():
            return
        assert self.db is not None
        flight_id = flight_id_for_plane(plane)
        alert_docs = alerts or []
        retained = self._should_retain(plane, alert_docs)
        if retained:
            self._persist_completed_flight(plane, flight_id, alert_docs)
            log.debug("Retained completed flight %s", flight_id)
        else:
            log.debug("Discarded uninteresting flight %s", flight_id)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
            self.db = None

    def _should_retain(self, plane: dict, alerts: list[dict[str, Any]]) -> bool:
        if alerts:
            return True

        recv = plane.get(STORE_RECV_DATA, {})
        calc = plane.get(STORE_CALC_DATA, {})
        if STORE_LAT not in recv or STORE_HEADING not in calc:
            return False

        internal = plane[STORE_INTERNAL]
        first_time = internal[STORE_FIRST_PACKET]
        last_time = internal[STORE_MOST_RECENT_PACKET]

        for zone_name, zone in self.config.zones.items():
            if not any(rule.retain for rule in zone.rules):
                continue
            polygon = self.polygons[zone_name]
            for rule in zone.rules:
                if not rule.retain:
                    continue
                valid = self._count_valid_ticks(
                    plane, zone_name, polygon, rule.when, first_time, last_time,
                )
                if valid >= rule.dwell_seconds:
                    return True
        return False

    def _count_valid_ticks(self, plane: dict, zone_name: str, polygon: Polygon,
                          when: dict, first_time: float, last_time: float) -> int:
        valid = 0
        for tick in range(int(first_time) + 1, int(last_time) + 1):
            lat = get_latest(STORE_RECV_DATA, STORE_LAT, plane, tick)
            lon = get_latest(STORE_RECV_DATA, STORE_LONG, plane, tick)
            heading = get_latest(STORE_CALC_DATA, STORE_HEADING, plane, tick)
            speed = get_latest(STORE_CALC_DATA, STORE_HORIZ_SPEED, plane, tick)
            if None in (lat, lon, heading, speed):
                continue
            position = (lat.value, lon.value)
            eta = geo.time_to_enter_geofence(position, heading.value, speed.value,
                                             polygon, _ETA_HORIZON)
            if eta is math.inf:
                eta = math.inf
            resolver = evaluate.make_resolver(plane, eta, polygon, position, tick)
            if evaluate.when_passes(when, resolver):
                valid += 1
        return valid

    def _persist_completed_flight(self, plane: dict, flight_id: str,
                                  alerts: list[dict[str, Any]]) -> None:
        assert self.db is not None
        info = plane.get(STORE_INFO, {})
        internal = plane[STORE_INTERNAL]
        icao = info[STORE_ICAO].lower()
        flight_doc = {
            "_id": flight_id,
            "icao": icao,
            "status": _FLIGHT_STATUS_COMPLETED,
            "active_alerts": [],
            "start_time": internal[STORE_FIRST_PACKET],
            "end_time": internal[STORE_MOST_RECENT_PACKET],
            "retained": True,
            "callsign": info.get(STORE_CALLSIGN) or "",
            "model": info.get("model") or "",
            "owner": info.get("owner") or "",
            "country": info.get("country") or "",
            "aircraft_type": info.get("aircraft_type") or info.get("typecode") or "",
            "typecode": info.get("typecode") or "",
            "info": {str(k): v for k, v in info.items()},
        }
        telemetry_docs = build_telemetry_docs(plane, flight_id, icao)
        alert_docs = [
            {
                "flight_id": flight_id,
                "icao": alert.get("icao", icao),
                "callsign": alert.get("callsign") or info.get(STORE_CALLSIGN) or "",
                "zone": alert.get("zone", ""),
                "rule": alert.get("rule", alert.get("level", "")),
                "active": alert.get("active", False),
                "activated_at": alert.get("activated_at", alert.get("timestamp")),
                "deactivated_at": alert.get("deactivated_at"),
                "eta": alert.get("eta"),
                "reason": alert.get("reason"),
                "last_updated": alert.get("last_updated"),
                "position": alert.get("position"),
                "altitude": alert.get("altitude"),
            }
            for alert in alerts
        ]

        try:
            self.db.get_collection("flights").replace_one(
                {"_id": flight_id},
                flight_doc,
                upsert=True,
            )
            if telemetry_docs:
                self.db.get_collection("telemetry").insert_many(telemetry_docs, ordered=False)
            if alert_docs:
                self.db.get_collection("alerts").insert_many(alert_docs, ordered=False)
        except PyMongoError as exc:
            log.error("Failed to persist completed flight %s: %s", flight_id, exc)
