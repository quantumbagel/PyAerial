"""Unified MongoDB persistence for live flights, telemetry, and alert events."""
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
    ALERT_CAT_ETA,
    ALERT_CAT_REASON,
    ALERT_CAT_TYPE,
    ALERT_CAT_ZONE,
    STORE_ALT,
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
)
from pyaerial.models import get_latest

log = logging.getLogger("pyaerial.store")

_RECONNECT_DELAY = 2.0
_ETA_HORIZON = 100_000
_FLIGHT_STATUS_LIVE = "live"
_FLIGHT_STATUS_COMPLETED = "completed"


def flight_id_for_plane(plane: dict) -> str:
    icao = plane[STORE_INFO][STORE_ICAO].lower()
    first_packet = plane[STORE_INTERNAL][STORE_FIRST_PACKET]
    return f"{icao}-{int(first_packet)}"


class MongoStore:
    """Single MongoDB writer for engine live updates, alerts, and finalization."""

    def __init__(self, config: Config, polygons: dict[str, Polygon]):
        self.config = config
        self.polygons = polygons
        self.uri = config.database.uri
        self.client: pymongo.MongoClient | None = None
        self.db: pymongo.database.Database | None = None
        self._last_telemetry_ts: dict[str, float] = {}
        self._connect()
        self.recover_stale_live_flights()

    def recover_stale_live_flights(self) -> None:
        """Mark live flights from a previous engine session as completed."""
        if not self._ensure_connected():
            return
        assert self.db is not None
        cutoff = time.time() - self.config.tracking.remember_planes
        try:
            result = self.db.get_collection("flights").update_many(
                {"status": _FLIGHT_STATUS_LIVE, "end_time": {"$lt": cutoff}},
                {"$set": {"status": _FLIGHT_STATUS_COMPLETED}},
            )
            if result.modified_count:
                log.info("Recovered %d stale live flight(s) from a previous session.",
                         result.modified_count)
        except PyMongoError as exc:
            log.warning("Could not recover stale live flights: %s", exc)

    def _connect(self) -> None:
        self.client = pymongo.MongoClient(
            self.uri,
            serverSelectionTimeoutMS=2000,
            connectTimeoutMS=1000,
            socketTimeoutMS=1000,
        )
        try:
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
        except PyMongoError as exc:
            log.error("Could not reach MongoDB at %s: %s", self.uri, exc)

    def _ensure_indexes(self) -> None:
        if self.db is None:
            return
        flights = self.db.get_collection("flights")
        flights.create_index([("icao", pymongo.ASCENDING)])
        flights.create_index([("status", pymongo.ASCENDING)])
        flights.create_index([("end_time", pymongo.DESCENDING)])

        telemetry = self.db.get_collection("telemetry")
        telemetry.create_index([("flight_id", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)])
        telemetry.create_index([("icao", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)])
        telemetry.create_index([("position", pymongo.GEOSPHERE)])

        alerts = self.db.get_collection("alerts")
        alerts.create_index([("timestamp", pymongo.DESCENDING)])
        alerts.create_index([("flight_id", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)])
        alerts.create_index([("icao", pymongo.ASCENDING), ("timestamp", pymongo.DESCENDING)])

    def _ensure_connected(self) -> bool:
        try:
            if self.client is not None:
                self.client.admin.command("ping")
                return self.db is not None
            return False
        except (PyMongoError, AttributeError):
            log.warning("Lost MongoDB connection; attempting to reconnect...")
            try:
                self._connect()
                if self.client is not None:
                    self.client.admin.command("ping")
                    return self.db is not None
                return False
            except PyMongoError:
                log.error("Reconnect to MongoDB failed.")
                time.sleep(_RECONNECT_DELAY)
                return False

    def write_live_planes(self, planes: dict[str, dict]) -> None:
        if not planes or not self._ensure_connected():
            return
        assert self.db is not None
        for plane in planes.values():
            self._upsert_live_flight(plane)

    def record_alert(self, plane: dict, meta: dict[str, Any], payload: dict[str, Any],
                     timestamp: float | None = None) -> None:
        if not self._ensure_connected():
            return
        assert self.db is not None
        flight_id = flight_id_for_plane(plane)
        event_time = timestamp or time.time()
        doc = {
            "flight_id": flight_id,
            "icao": meta[STORE_ICAO].lower(),
            "callsign": meta.get(STORE_CALLSIGN) or "",
            "zone": meta.get(ALERT_CAT_ZONE, ""),
            "level": meta.get(ALERT_CAT_TYPE, ""),
            "eta": meta.get(ALERT_CAT_ETA),
            "reason": meta.get(ALERT_CAT_REASON),
            "timestamp": event_time,
            "position": {
                "type": "Point",
                "coordinates": [payload.get(STORE_LONG), payload.get(STORE_LAT)],
            },
            "altitude": payload.get(STORE_ALT),
        }
        try:
            self.db.get_collection("alerts").insert_one(doc)
        except PyMongoError as exc:
            log.error("Failed to record alert for %s: %s", flight_id, exc)

    def finalize_plane(self, plane: dict) -> None:
        if not self._ensure_connected():
            return
        assert self.db is not None
        flight_id = flight_id_for_plane(plane)
        retained = self._should_retain(plane, flight_id)
        if retained:
            self._mark_completed(plane, flight_id, retained=True)
            log.debug("Retained completed flight %s", flight_id)
        else:
            self._delete_flight_data(flight_id, plane[STORE_INFO][STORE_ICAO].lower())
            log.debug("Discarded uninteresting flight %s", flight_id)
        self._last_telemetry_ts.pop(flight_id, None)

    def finalize_all_live(self) -> None:
        if not self._ensure_connected():
            return
        assert self.db is not None
        cursor = self.db.get_collection("flights").find({"status": _FLIGHT_STATUS_LIVE}, {"_id": 1})
        for doc in cursor:
            flight_id = doc["_id"]
            self.db.get_collection("flights").update_one(
                {"_id": flight_id},
                {"$set": {"status": _FLIGHT_STATUS_COMPLETED, "end_time": time.time()}},
            )

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
            self.db = None

    def _upsert_live_flight(self, plane: dict) -> None:
        assert self.db is not None
        info = plane.get(STORE_INFO, {})
        internal = plane.get(STORE_INTERNAL, {})
        flight_id = flight_id_for_plane(plane)
        icao = info[STORE_ICAO].lower()
        first_packet = internal[STORE_FIRST_PACKET]
        last_packet = internal[STORE_MOST_RECENT_PACKET]

        flight_doc = {
            "_id": flight_id,
            "icao": icao,
            "status": _FLIGHT_STATUS_LIVE,
            "zone": plane.get("zone") or "",
            "level": plane.get("level") or "",
            "start_time": first_packet,
            "end_time": last_packet,
            "retained": False,
            "callsign": info.get(STORE_CALLSIGN) or "",
            "model": info.get("model") or "",
            "owner": info.get("owner") or "",
            "country": info.get("country") or "",
            "info": {str(k): v for k, v in info.items()},
            "raw_messages": plane.get("raw_messages", []),
        }
        try:
            self.db.get_collection("flights").replace_one({"_id": flight_id}, flight_doc, upsert=True)
            self._write_telemetry_points(plane, flight_id, icao)
        except PyMongoError as exc:
            log.error("Failed to upsert live flight %s: %s", flight_id, exc)

    def _write_telemetry_points(self, plane: dict, flight_id: str, icao: str) -> None:
        assert self.db is not None
        last_written = self._last_telemetry_ts.get(flight_id, 0.0)
        lat_series = plane.get(STORE_RECV_DATA, {}).get(STORE_LAT, [])
        if not lat_series:
            return

        docs: list[dict[str, Any]] = []
        for lat_datum in lat_series:
            if lat_datum.time <= last_written:
                continue
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
            last_written = max(last_written, lat_datum.time)

        if not docs:
            return
        try:
            self.db.get_collection("telemetry").insert_many(docs, ordered=False)
            self._last_telemetry_ts[flight_id] = last_written
        except PyMongoError as exc:
            if getattr(exc, "details", {}).get("writeErrors"):
                self._last_telemetry_ts[flight_id] = last_written
            else:
                log.error("Failed to write telemetry for %s: %s", flight_id, exc)

    def _should_retain(self, plane: dict, flight_id: str) -> bool:
        assert self.db is not None
        if self.db.get_collection("alerts").count_documents({"flight_id": flight_id}, limit=1):
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

    def _mark_completed(self, plane: dict, flight_id: str, *, retained: bool) -> None:
        assert self.db is not None
        internal = plane[STORE_INTERNAL]
        self.db.get_collection("flights").update_one(
            {"_id": flight_id},
            {"$set": {
                "status": _FLIGHT_STATUS_COMPLETED,
                "end_time": internal[STORE_MOST_RECENT_PACKET],
                "retained": retained,
                "zone": plane.get("zone") or "",
                "level": plane.get("level") or "",
                "raw_messages": plane.get("raw_messages", []),
            }},
        )

    def _delete_flight_data(self, flight_id: str, icao: str) -> None:
        assert self.db is not None
        self.db.get_collection("flights").delete_one({"_id": flight_id})
        self.db.get_collection("telemetry").delete_many({"flight_id": flight_id})
        self.db.get_collection("alerts").delete_many({"flight_id": flight_id})
