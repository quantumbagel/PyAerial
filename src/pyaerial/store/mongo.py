"""MongoDB persistence for retained historical flights, telemetry, and alerts."""

from __future__ import annotations

import logging
import time
from typing import Any

import pymongo
from pymongo.errors import PyMongoError
from shapely import Polygon

from pyaerial.alerts.retain import should_retain
from pyaerial.config.schema import Config
from pyaerial.constants import (
    STORE_CALLSIGN,
    STORE_FIRST_PACKET,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_MOST_RECENT_PACKET,
)
from pyaerial.models import flight_id_for_plane, iter_telemetry_samples

log = logging.getLogger("pyaerial.store")

_RECONNECT_DELAY = 2.0
_FLIGHT_STATUS_COMPLETED = "completed"


def build_telemetry_docs(
    plane: dict, flight_id: str, icao: str
) -> list[dict[str, Any]]:
    """Build telemetry documents from a plane's in-memory time series."""
    docs: list[dict[str, Any]] = []
    for timestamp, lat, lon, alt, speed, heading in iter_telemetry_samples(plane):
        doc: dict[str, Any] = {
            "flight_id": flight_id,
            "icao": icao,
            "timestamp": timestamp,
            "latitude": lat,
            "longitude": lon,
            "position": {
                "type": "Point",
                "coordinates": [lon, lat],
            },
        }
        if alt is not None:
            doc["altitude"] = alt
        if speed is not None:
            doc["speed"] = speed
        if heading is not None:
            doc["heading"] = heading
        docs.append(doc)
    return docs


class MongoStore:
    """MongoDB writer for retained completed flights only."""

    def __init__(
        self,
        config: Config,
        polygons: dict[str, Polygon],
        *,
        disabled: bool = False,
    ):
        self.config = config
        self.polygons = polygons
        self.uri = config.database.uri
        self.disabled = disabled
        self.client: pymongo.MongoClient | None = None
        self.db: pymongo.database.Database | None = None
        self._last_connect_attempt = 0.0
        self._reported_down = False
        if not disabled:
            self._connect()

    def _connect(self) -> None:
        try:
            self.client = pymongo.MongoClient(
                self.uri,
                serverSelectionTimeoutMS=2000,
                connectTimeoutMS=2000,
                socketTimeoutMS=5000,
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
            if self._reported_down:
                log.info(
                    "Reconnected to MongoDB database '%s' at %s", self.db.name, self.uri
                )
            else:
                log.info(
                    "Connected to MongoDB database '%s' at %s", self.db.name, self.uri
                )
            self._reported_down = False
        except Exception:
            if not self._reported_down:
                log.info(
                    "MongoDB unavailable at %s; operating in offline mode.", self.uri
                )
                self._reported_down = True
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
        flights.create_index(
            [
                ("status", pymongo.ASCENDING),
                ("retained", pymongo.ASCENDING),
                ("end_time", pymongo.DESCENDING),
            ]
        )

        telemetry = self.db.get_collection("telemetry")
        telemetry.create_index(
            [("flight_id", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)]
        )
        telemetry.create_index(
            [("icao", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)]
        )
        telemetry.create_index([("position", pymongo.GEOSPHERE)])

        alerts = self.db.get_collection("alerts")
        alerts.create_index([("timestamp", pymongo.DESCENDING)])
        alerts.create_index(
            [("flight_id", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)]
        )
        alerts.create_index(
            [("icao", pymongo.ASCENDING), ("timestamp", pymongo.DESCENDING)]
        )
        alerts.create_index([("activated_at", pymongo.DESCENDING)])

    def _ensure_connected(self) -> bool:
        if self.disabled:
            return False
        if self.client is None or self.db is None:
            now = time.monotonic()
            if now - self._last_connect_attempt >= _RECONNECT_DELAY:
                self._last_connect_attempt = now
                self._connect()
        if self.client is None or self.db is None:
            return False
        try:
            self.client.admin.command("ping")
            return True
        except (PyMongoError, AttributeError):
            if not self._reported_down:
                log.info("Lost MongoDB connection; operating in offline mode.")
                self._reported_down = True
            self.client = None
            self.db = None
            return False

    def finalize_plane(
        self, plane: dict, *, alerts: list[dict[str, Any]] | None = None
    ) -> bool:
        """Persist a completed flight to Mongo if retention rules are met.

        Returns True when it is safe to drop the live Redis copy: the flight
        was written, was intentionally discarded, or persistence is disabled.
        Returns False when the flight should have been written but Mongo was
        unavailable — the caller must keep the live data and retry.
        """
        if self.disabled:
            return True
        alert_docs = alerts or []
        retained = should_retain(plane, alert_docs, self.config, self.polygons)
        if not retained:
            log.debug("Discarded uninteresting flight %s", flight_id_for_plane(plane))
            return True
        if not self._ensure_connected():
            return False
        assert self.db is not None
        flight_id = flight_id_for_plane(plane)
        if self._persist_completed_flight(plane, flight_id, alert_docs):
            log.debug("Retained completed flight %s", flight_id)
            return True
        return False

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
            self.db = None

    def _persist_completed_flight(
        self, plane: dict, flight_id: str, alerts: list[dict[str, Any]]
    ) -> bool:
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
            "aircraft_type": info.get("aircraft_type") or "",
            "info": {str(k): v for k, v in info.items()},
        }
        telemetry_docs = build_telemetry_docs(plane, flight_id, icao)
        alert_docs = [
            {
                "_id": alert.get("alert_id")
                or f"{flight_id}:{alert.get('zone', '')}:{alert.get('rule', '')}",
                "alert_id": alert.get("alert_id")
                or f"{flight_id}:{alert.get('zone', '')}:{alert.get('rule', '')}",
                "flight_id": flight_id,
                "icao": alert.get("icao", icao),
                "callsign": alert.get("callsign") or info.get(STORE_CALLSIGN) or "",
                "zone": alert.get("zone", ""),
                "rule": alert.get("rule", ""),
                "active": alert.get("active", False),
                "activated_at": alert.get("activated_at"),
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
            # Telemetry and alerts first so a crash cannot leave a flight
            # document without its track.
            if telemetry_docs:
                telemetry_col = self.db.get_collection("telemetry")
                telemetry_col.delete_many({"flight_id": flight_id})
                telemetry_col.insert_many(telemetry_docs, ordered=False)
            if alert_docs:
                alerts_col = self.db.get_collection("alerts")
                for adoc in alert_docs:
                    alerts_col.replace_one(
                        {"_id": adoc["_id"]},
                        adoc,
                        upsert=True,
                    )
            self.db.get_collection("flights").replace_one(
                {"_id": flight_id},
                flight_doc,
                upsert=True,
            )
            return True
        except PyMongoError as exc:
            log.error("Failed to persist completed flight %s: %s", flight_id, exc)
            return False
