"""Saver that persists eligible flights to MongoDB in a webapp-friendly schema."""
from __future__ import annotations

import time

import pymongo
from pymongo.errors import PyMongoError
from shapely import Polygon

from pyaerial.config.schema import Config
from pyaerial.constants import (
    STORAGE_CATEGORY,
    STORAGE_DATA,
    STORAGE_DATA_TYPE,
    STORAGE_LEVEL,
    STORAGE_ZONE,
    STORE_CALC_DATA,
    STORE_FIRST_PACKET,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_PACKET_TYPE,
    STORE_RECV_DATA,
    STORE_MOST_RECENT_PACKET,
    STORE_ICAO,
)
from pyaerial.savers import Saver, register_saver

_RECONNECT_DELAY = 2.0


@register_saver("mongo")
class MongoSaver(Saver):
    def __init__(self, config: Config, polygons: dict[str, Polygon]):
        super().__init__(config, polygons)
        self.uri = config.general.mongodb
        self.client: pymongo.MongoClient | None = None
        self.db: pymongo.database.Database | None = None
        self._connect()

    def _connect(self) -> None:
        self.client = pymongo.MongoClient(
            self.uri, serverSelectionTimeoutMS=2000,
            connectTimeoutMS=1000, socketTimeoutMS=1000)
        try:
            self.client.admin.command("ping")
            
            # Select target database, respecting URI or defaulting to 'pyaerial'
            try:
                self.db = self.client.get_default_database()
            except Exception:
                self.db = self.client.get_database("pyaerial")
            
            # Ensure indexes exist for fast telemetry querying and spatial lookups
            self.db.get_collection("flights").create_index([("icao", pymongo.ASCENDING)])
            self.db.get_collection("telemetry").create_index([
                ("flight_id", pymongo.ASCENDING),
                ("timestamp", pymongo.ASCENDING)
            ])
            self.db.get_collection("telemetry").create_index([
                ("icao", pymongo.ASCENDING),
                ("timestamp", pymongo.ASCENDING)
            ])
            self.db.get_collection("telemetry").create_index([("position", pymongo.GEOSPHERE)])
            
            self.logger.info("Connected to MongoDB database '%s' at %s", self.db.name, self.uri)
        except PyMongoError as exc:
            self.logger.error("Could not reach MongoDB at %s; will retry when saving. Error: %s", self.uri, exc)

    def _ensure_connected(self) -> bool:
        try:
            if self.client is not None:
                self.client.admin.command("ping")
                return self.db is not None
            return False
        except (PyMongoError, AttributeError):
            self.logger.warning("Lost MongoDB connection; attempting to reconnect...")
            try:
                self._connect()
                if self.client is not None:
                    self.client.admin.command("ping")
                    return self.db is not None
                return False
            except PyMongoError:
                self.logger.error("Reconnect to MongoDB failed.")
                time.sleep(_RECONNECT_DELAY)
                return False

    def save(self) -> None:
        if not self._cache:
            return
        if not self._ensure_connected():
            self.logger.error("Skipping save of %d flight-level(s): MongoDB unavailable.",
                               len(self._cache))
            return

        self.logger.info("Saving %d eligible flight-level(s).", len(self._cache))
        for (icao, zone, level), data in self._cache.items():
            self._save_flight(icao, zone, level, data)

        self.logger.info("Done saving %d flight-level(s).", len(self._cache))
        self._cache = {}

    def _save_flight(self, icao: str, zone: str, level: str, data: dict) -> None:
        if self.db is None:
            return

        internal = data[STORE_INTERNAL]
        internal[STORE_PACKET_TYPE] = {
            str(k): v for k, v in internal.get(STORE_PACKET_TYPE, {}).items()
        }

        # Unique identifier for the flight
        flight_id = f"{icao.lower()}-{int(internal[STORE_FIRST_PACKET])}-{zone}-{level}"

        # Flight document metadata
        flight_doc = {
            "_id": flight_id,
            "icao": icao.lower(),
            "zone": zone,
            "level": level,
            "start_time": internal[STORE_FIRST_PACKET],
            "end_time": internal[STORE_MOST_RECENT_PACKET],
            "info": {str(k): v for k, v in data[STORE_INFO].items()},
            "internal": {str(k): v for k, v in internal.items()},
            "raw_messages": data.get("raw_messages", [])
        }

        # Group telemetry data by timestamp
        telemetry_by_time = {}
        for bucket in (STORE_RECV_DATA, STORE_CALC_DATA):
            for field, series in data.get(bucket, {}).items():
                for datum in series:
                    t = datum.time
                    if t not in telemetry_by_time:
                        telemetry_by_time[t] = {}
                    telemetry_by_time[t][field] = datum.value

        # Build individual telemetry documents
        telemetry_docs = []
        for t, fields in telemetry_by_time.items():
            doc = {
                "flight_id": flight_id,
                "icao": icao.lower(),
                "timestamp": t,
            }
            # Handle spatial index using GeoJSON (longitude, latitude)
            if "latitude" in fields and "longitude" in fields:
                doc["position"] = {
                    "type": "Point",
                    "coordinates": [fields["longitude"], fields["latitude"]]
                }
            # Include other metrics
            for field, value in fields.items():
                if field not in ("latitude", "longitude"):
                    doc[field] = value
            telemetry_docs.append(doc)

        try:
            # Save flight metadata (upsert/replace)
            self.db.get_collection("flights").replace_one({"_id": flight_id}, flight_doc, upsert=True)

            # Save flight telemetry (repopulate to support idempotency/updates)
            if telemetry_docs:
                self.db.get_collection("telemetry").delete_many({"flight_id": flight_id})
                self.db.get_collection("telemetry").insert_many(telemetry_docs)

            self.logger.info("Successfully saved metadata and %d telemetry points for %s.",
                             len(telemetry_docs), flight_id)
        except PyMongoError as exc:
            self.logger.error("Failed to save flight %s (%s/%s): %s", icao, zone, level, exc)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
            self.db = None

