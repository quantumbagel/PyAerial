"""
Per-plane tick orchestrator: kinematics, metadata lookup, then geofence alerts.

Motion lives in :mod:`pyaerial.calc.kinematics`. Alert lifecycle lives in
:mod:`pyaerial.alerts.engine`. This module wires them for the engine loop.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import time
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
        self._pending_results: dict[str, dict] = {}
        self._lookup_backoff_until: dict[str, float] = {}
        self._lookup_failures: dict[str, int] = {}
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
        self._apply_lookup_result(plane)
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

    def _apply_lookup_result(self, plane: dict) -> None:
        info = plane.get(STORE_INFO) or {}
        icao = info.get(STORE_ICAO)
        if not icao:
            return
        with self._lock:
            result = self._pending_results.pop(icao, None)
        if not result:
            return
        live_cs = info.get(STORE_CALLSIGN)
        if result.get("callsign") and not live_cs:
            info[STORE_CALLSIGN] = result["callsign"]
        if result.get("resolved"):
            for field in ("model", "owner", "country", "aircraft_type"):
                if result.get(field):
                    info[field] = result[field]
            info["metadata_resolved"] = True

    def _resolve_callsign(self, plane: dict) -> str:
        info = plane[STORE_INFO]
        if info.get("metadata_resolved"):
            return info.get(STORE_CALLSIGN) or ""

        icao = info[STORE_ICAO]
        now = time.time()
        with self._lock:
            if icao in self._pending_lookups or now < self._lookup_backoff_until.get(
                icao, 0.0
            ):
                return info.get(STORE_CALLSIGN) or ""
            self._pending_lookups.add(icao)

        self._executor.submit(
            self._bg_lookup_metadata, icao, info.get(STORE_CALLSIGN) or ""
        )
        return info.get(STORE_CALLSIGN) or ""

    def _bg_lookup_metadata(self, icao: str, live_callsign: str = "") -> None:
        callsign = live_callsign
        model = ""
        owner = ""
        country = ""
        aircraft_type = ""
        resolved = True
        try:
            if self.aircraft_db and self.aircraft_db.available:
                record = self.aircraft_db.lookup_cached(icao)
                if record:
                    if not callsign:
                        callsign = record.get("callsign") or record.get("registration")
                    model = record.get("model") or ""
                    owner = record.get("owner") or ""
                    country = record.get("country") or ""
                    aircraft_type = record.get("typecode") or ""
                elif not self.aircraft_db.is_cached(icao):
                    resolved = False
        except Exception as exc:
            log.debug("Background metadata lookup failed for %s: %s", icao, exc)
            resolved = False
        with self._lock:
            self._pending_results[icao] = {
                "callsign": callsign or "",
                "model": model,
                "owner": owner,
                "country": country,
                "aircraft_type": aircraft_type,
                "resolved": resolved,
            }
            if resolved:
                self._lookup_failures.pop(icao, None)
                self._lookup_backoff_until.pop(icao, None)
            else:
                failures = self._lookup_failures.get(icao, 0) + 1
                self._lookup_failures[icao] = failures
                self._lookup_backoff_until[icao] = time.time() + min(
                    60.0, 2.0 ** failures
                )
            self._pending_lookups.discard(icao)
