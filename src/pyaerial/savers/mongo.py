"""Saver that persists eligible flights to MongoDB."""
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
)
from pyaerial.savers import Saver, register_saver

_RECONNECT_DELAY = 2.0


@register_saver("mongo")
class MongoSaver(Saver):
    def __init__(self, config: Config, polygons: dict[str, Polygon]):
        super().__init__(config, polygons)
        self.uri = config.general.mongodb
        self.client: pymongo.MongoClient | None = None
        self._connect()

    def _connect(self) -> None:
        self.client = pymongo.MongoClient(
            self.uri, serverSelectionTimeoutMS=2000,
            connectTimeoutMS=1000, socketTimeoutMS=1000)
        try:
            self.client.admin.command("ping")
            self.logger.info("Connected to MongoDB at %s", self.uri)
        except PyMongoError:
            self.logger.error("Could not reach MongoDB at %s; will retry when saving.", self.uri)

    def _ensure_connected(self) -> bool:
        try:
            self.client.admin.command("ping")
            return True
        except PyMongoError:
            self.logger.warning("Lost MongoDB connection; attempting to reconnect...")
            try:
                self._connect()
                self.client.admin.command("ping")
                return True
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
        internal = data[STORE_INTERNAL]
        internal[STORE_PACKET_TYPE] = {
            str(k): v for k, v in internal.get(STORE_PACKET_TYPE, {}).items()
        }

        database = self.client.get_database(icao.lower())
        collection_name = f"{int(internal[STORE_FIRST_PACKET])}-{zone}-{level}"
        collection = database.get_collection(collection_name)

        documents = []
        for bucket in (STORE_RECV_DATA, STORE_CALC_DATA):
            for field, series in data.get(bucket, {}).items():
                documents.append({
                    STORAGE_CATEGORY: bucket,
                    STORAGE_DATA_TYPE: field,
                    STORAGE_DATA: [[datum.time, datum.value] for datum in series],
                })

        info_document = {STORAGE_CATEGORY: STORE_INFO, STORAGE_ZONE: zone, STORAGE_LEVEL: level}
        for info_bucket in (STORE_INFO, STORE_INTERNAL):
            info_document.update({str(k): v for k, v in data[info_bucket].items()})
        documents.append(info_document)

        try:
            if documents:
                collection.insert_many(documents)
        except PyMongoError as exc:
            self.logger.error("Failed to save flight %s (%s/%s): %s", icao, zone, level, exc)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
