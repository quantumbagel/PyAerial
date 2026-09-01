"""
Per-plane tick orchestrator: kinematics, metadata lookup, then geofence alerts.

Motion lives in :mod:`pyaerial.calc.kinematics`. Alert lifecycle lives in
:mod:`pyaerial.alerts.engine`. This module wires them for the engine loop.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import threading
from typing import TYPE_CHECKING

from shapely import Polygon

from pyaerial.alerts.engine import AlertEngine
from pyaerial.calc.kinematics import Kinematics
from pyaerial.config.schema import Config
from pyaerial.constants import STORE_CALLSIGN, STORE_ICAO, STORE_INFO
from pyaerial.enrich.aircraft_db import AircraftDB

if TYPE_CHECKING:
    from pyaerial.store.redis_live import RedisLiveStore

log = logging.getLogger("pyaerial.calc.plane")


class PlaneCalculator:
    """Per-tick facade: kinematics → metadata → alerts."""

    def __init__(
        self,
        config: Config,
        polygons: dict[str, Polygon],
        aircraft_db: AircraftDB | None = None,
        store: RedisLiveStore | None = None,
    ):
        self.config = config
        self.polygons = polygons
        self.aircraft_db = aircraft_db
        self.store = store
        self.kinematics = Kinematics(config)
        self.alerts = AlertEngine(config, polygons, store)
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="callsign-lookup"
        )
        self._pending_lookups: set[str] = set()
        self._lock = threading.Lock()

    @property
    def _kalman_filters(self):
        return self.kinematics._kalman_filters

    @property
    def _smoothed_turn_rates(self):
        return self.kinematics._smoothed_turn_rates

    @property
    def _alert_state(self):
        return self.alerts._alert_state

    @property
    def _pending_match(self):
        return self.alerts._pending_match

    def close(self) -> None:
        self.alerts.close()
        self._executor.shutdown(wait=True, cancel_futures=True)
        self.kinematics.close()

    def calculate_all(self, planes: dict[str, dict]) -> None:
        # Always evaluate every plane with a position so ETA can coast during
        # ADS-B gaps.
        for plane in planes.values():
            self.calculate_plane(plane)

    def forget_motion(self, icao: str) -> None:
        self.kinematics.forget(icao)

    def calculate_plane(self, plane: dict) -> None:
        update = self.kinematics.update(plane)
        if update is None:
            return
        callsign = self._resolve_callsign(plane)
        self.alerts.check(plane, update.alert_position, update.motion, callsign)

    def deactivate_plane(self, plane: dict) -> None:
        """Deactivate alerts and drop motion state for an expired plane."""
        info = plane.get(STORE_INFO, {})
        icao = info.get(STORE_ICAO)
        self.alerts.deactivate(plane)
        if icao:
            self.kinematics.forget(icao.lower())

    def _resolve_callsign(self, plane: dict) -> str:
        info = plane[STORE_INFO]
        if info.get("metadata_resolved"):
            return info.get(STORE_CALLSIGN) or ""

        icao = info[STORE_ICAO]
        with self._lock:
            if icao in self._pending_lookups:
                return info.get(STORE_CALLSIGN) or ""
            self._pending_lookups.add(icao)

        self._executor.submit(self._bg_lookup_metadata, plane, icao)
        return info.get(STORE_CALLSIGN) or ""

    def _bg_lookup_metadata(self, plane: dict, icao: str) -> None:
        try:
            callsign = plane[STORE_INFO].get(STORE_CALLSIGN)
            model = ""
            owner = ""
            country = ""
            aircraft_type = ""

            if self.aircraft_db and self.aircraft_db.available:
                record = self.aircraft_db.lookup_cached(icao)
                if record:
                    if not callsign:
                        callsign = record.get("callsign") or record.get("registration")
                    model = record.get("model") or ""
                    owner = record.get("owner") or ""
                    country = record.get("country") or ""
                    aircraft_type = record.get("typecode") or ""

            with self._lock:
                live_cs = plane[STORE_INFO].get(STORE_CALLSIGN)
                if live_cs:
                    callsign = live_cs
                plane[STORE_INFO][STORE_CALLSIGN] = callsign or ""
                plane[STORE_INFO]["model"] = model
                plane[STORE_INFO]["owner"] = owner
                plane[STORE_INFO]["country"] = country
                plane[STORE_INFO]["aircraft_type"] = aircraft_type
                plane[STORE_INFO]["metadata_resolved"] = True
        except Exception as exc:
            log.debug("Background metadata lookup failed for %s: %s", icao, exc)
            with self._lock:
                plane[STORE_INFO].setdefault(STORE_CALLSIGN, "")
                plane[STORE_INFO].setdefault("model", "")
                plane[STORE_INFO].setdefault("owner", "")
                plane[STORE_INFO].setdefault("country", "")
                plane[STORE_INFO].setdefault("aircraft_type", "")
                plane[STORE_INFO]["metadata_resolved"] = True
        finally:
            with self._lock:
                self._pending_lookups.discard(icao)
