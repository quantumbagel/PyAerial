"""
Per-plane tick orchestrator: kinematics, metadata lookup, then geofence alerts.

Motion lives in :mod:`pyaerial.calc.kinematics`. Alert lifecycle lives in
:mod:`pyaerial.alerts.engine`. This module wires them for the engine loop.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import TYPE_CHECKING, Any

from shapely import Polygon

from pyaerial.alerts.engine import AlertEngine
from pyaerial.calc.kinematics import Kinematics
from pyaerial.config.schema import Config
from pyaerial.enrich.aircraft_db import AircraftDB
from pyaerial.models import Plane

if TYPE_CHECKING:
    from pyaerial.store.live import EpisodeStore

log = logging.getLogger("pyaerial.pipeline")

_LOOKUP_WORKERS = 4


class PlaneCalculator:
    """Per-tick facade: kinematics → metadata → alerts."""

    def __init__(
        self,
        config: Config,
        polygons: dict[str, Polygon],
        aircraft_db: AircraftDB | None = None,
        store: EpisodeStore | None = None,
    ):
        self.config = config
        self.polygons = polygons
        self.aircraft_db = aircraft_db
        self.store = store
        # enrichment.enabled: false -> local aircraft.db cache only, never
        # hexdb.io / Planespotters.
        self._online_lookups = config.enrichment.enabled
        self.kinematics = Kinematics(config)
        self.alerts = AlertEngine(config, polygons, store)
        # Daemon threads, not a ThreadPoolExecutor: executor workers are joined
        # at interpreter exit, so an in-flight HTTP lookup (up to ~9 s) would
        # stall SIGTERM past Docker's 10 s kill. Started on first lookup.
        self._lookup_queue: queue.Queue[tuple[str, str] | None] = queue.Queue()
        self._lookup_threads: list[threading.Thread] = []
        self._closed = False
        self._pending_lookups: set[str] = set()
        # In-flight lookups whose plane expired; their result is discarded.
        self._cancelled_lookups: set[str] = set()
        self._pending_results: dict[str, dict[str, Any]] = {}
        # time.monotonic() deadlines
        self._lookup_backoff_until: dict[str, float] = {}
        self._lookup_failures: dict[str, int] = {}
        self._lock = threading.Lock()
        # ICAOs whose last tick raised; logged with a traceback once per streak.
        self._failing: set[str] = set()

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
        self._stop_lookups()
        self.kinematics.close()

    def _stop_lookups(self) -> None:
        """Drop queued lookups and return without waiting for in-flight ones."""
        with self._lock:
            self._closed = True
            self._pending_lookups.clear()
            self._pending_results.clear()
        while True:
            try:
                self._lookup_queue.get_nowait()
            except queue.Empty:
                break
        for _ in self._lookup_threads:
            self._lookup_queue.put(None)

    def _lookup_worker(self) -> None:
        while True:
            job = self._lookup_queue.get()
            if job is None or self._closed:
                return
            self._bg_lookup_metadata(*job)

    def calculate_all(self, planes: dict[str, Plane]) -> None:
        # Always evaluate every plane with a position so ETA can coast during
        # ADS-B gaps.
        for key, plane in planes.items():
            # One plane with bad data must not stop alerting for the others.
            try:
                self.calculate_plane(plane)
            except Exception:
                if key in self._failing:
                    log.debug("Plane %s calculation failed again", key, exc_info=True)
                else:
                    self._failing.add(key)
                    log.exception("Plane %s calculation failed; skipping it", key)
            else:
                self._failing.discard(key)

    def handover(self, plane: Plane, old_flight_id: str, new_flight_id: str) -> None:
        """Flight segmentation: continue *plane*'s alerts under *new_flight_id*.

        See :meth:`AlertEngine.handover`. Motion and lookup state are per
        ICAO and simply carry on.
        """
        self.alerts.handover(plane, old_flight_id, new_flight_id)

    def forget_motion(self, icao: str) -> None:
        self.kinematics.forget(icao)

    def calculate_plane(self, plane: Plane) -> None:
        self._apply_lookup_result(plane)
        update = self.kinematics.update(plane)
        if update is None:
            return
        callsign = self._resolve_callsign(plane)
        self.alerts.check(plane, update.alert_position, update.motion, callsign)

    def deactivate_plane(self, plane: Plane) -> None:
        """Deactivate alerts and drop motion state for an expired plane.

        Per-ICAO state (motion, lookups) is kept when another flight of the
        same aircraft is live, e.g. when the orphan sweep archives an old
        live copy of an aircraft that is being tracked again.
        """
        icao = plane.info.icao
        if not self.alerts.deactivate(plane):
            return
        if icao:
            self._failing.discard(icao)
            self.kinematics.forget(icao.lower())
            # Per-ICAO lookup state would otherwise grow with every plane ever seen.
            with self._lock:
                self._pending_results.pop(icao, None)
                self._lookup_backoff_until.pop(icao, None)
                self._lookup_failures.pop(icao, None)
                if icao in self._pending_lookups:
                    self._cancelled_lookups.add(icao)

    def _apply_lookup_result(self, plane: Plane) -> None:
        info = plane.info
        icao = info.icao
        if not icao:
            return
        with self._lock:
            result = self._pending_results.pop(icao, None)
        if not result:
            return
        if result.get("callsign") and not info.callsign:
            info.callsign = result["callsign"]
        if result.get("resolved"):
            # In this order: it is the order of the archived info document.
            if result.get("model"):
                info.model = result["model"]
            if result.get("owner"):
                info.owner = result["owner"]
            if result.get("country"):
                info.country = result["country"]
            if result.get("aircraft_type"):
                info.aircraft_type = result["aircraft_type"]
            if result.get("registration"):
                info.registration = result["registration"]
            info.metadata_resolved = True

    def _resolve_callsign(self, plane: Plane) -> str:
        info = plane.info
        if info.metadata_resolved:
            return info.callsign or ""

        icao = info.icao
        now = time.monotonic()
        with self._lock:
            if (
                self._closed
                or icao in self._pending_lookups
                or now < self._lookup_backoff_until.get(icao, 0.0)
            ):
                return info.callsign or ""
            self._pending_lookups.add(icao)
            self._cancelled_lookups.discard(icao)
            if not self._lookup_threads:
                self._lookup_threads = [
                    threading.Thread(
                        target=self._lookup_worker,
                        name=f"callsign-lookup-{i}",
                        daemon=True,
                    )
                    for i in range(_LOOKUP_WORKERS)
                ]
                for thread in self._lookup_threads:
                    thread.start()

        self._lookup_queue.put((icao, info.callsign or ""))
        return info.callsign or ""

    def _bg_lookup_metadata(self, icao: str, live_callsign: str = "") -> None:
        callsign = live_callsign
        model = ""
        owner = ""
        country = ""
        aircraft_type = ""
        registration = ""
        resolved = True
        try:
            if self.aircraft_db and self.aircraft_db.available:
                if self._online_lookups:
                    record = self.aircraft_db.lookup_cached(icao)
                else:
                    # Cache only; a miss is final (nothing to retry offline).
                    record = self.aircraft_db.lookup_cached_fast(icao)
                if record:
                    if not callsign:
                        callsign = (
                            record.get("callsign") or record.get("registration") or ""
                        )
                    model = record.get("model") or ""
                    owner = record.get("owner") or ""
                    country = record.get("country") or ""
                    aircraft_type = record.get("typecode") or ""
                    registration = record.get("registration") or ""
                elif self._online_lookups and not self.aircraft_db.is_cached(icao):
                    resolved = False
        except Exception as exc:
            log.debug("Background metadata lookup failed for %s: %s", icao, exc)
            resolved = False
        with self._lock:
            self._pending_lookups.discard(icao)
            if self._closed:
                # Finished after close (aircraft DB may already be shut).
                self._cancelled_lookups.discard(icao)
                return
            if icao in self._cancelled_lookups:
                self._cancelled_lookups.discard(icao)
                return
            self._pending_results[icao] = {
                "callsign": callsign or "",
                "model": model,
                "owner": owner,
                "country": country,
                "aircraft_type": aircraft_type,
                "registration": registration,
                "resolved": resolved,
            }
            if resolved:
                self._lookup_failures.pop(icao, None)
                self._lookup_backoff_until.pop(icao, None)
            else:
                failures = self._lookup_failures.get(icao, 0) + 1
                self._lookup_failures[icao] = failures
                self._lookup_backoff_until[icao] = time.monotonic() + min(
                    60.0, 2.0**failures
                )
