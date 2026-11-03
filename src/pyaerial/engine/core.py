"""The tick loop: orchestrates receivers, tracking, calculations, alerting and
persistence, with stage guards, the heartbeat, signals and lifecycle.

Receiver threads and the intake queue live in :mod:`.supervisor`; archiving,
recovery and segmentation in :mod:`.archiver`. The tick thread owns
``tracker.planes``; receivers hand frames over only through the queue.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import threading
import time
from collections.abc import Callable
from types import FrameType
from typing import Any

from shapely import Polygon

from pyaerial.alerters import available_alerters
from pyaerial.calc.geo import build_polygons
from pyaerial.config.schema import Config
from pyaerial.constants import DEFAULT_AIRCRAFT_DB
from pyaerial.engine.archiver import FlightArchiver
from pyaerial.engine.guard import StageGuard
from pyaerial.engine.supervisor import ReceiverSupervisor
from pyaerial.enrich.aircraft_db import AircraftDB
from pyaerial.integrity import IntegrityMonitor
from pyaerial.logging_setup import setup_logging
from pyaerial.pipeline import PlaneCalculator
from pyaerial.receivers import RawFrame, available_receivers
from pyaerial.receivers.positions import ReceiverPositions
from pyaerial.store import ArchiveWriter
from pyaerial.store.live import LiveWriter, LiveWriteStore
from pyaerial.store.recorder import EpisodeRecorder
from pyaerial.tracker import Tracker

log = logging.getLogger("pyaerial.engine")

_SignalHandler = Callable[[int, FrameType | None], Any] | int | signal.Handlers | None


class Engine:
    """Orchestrates receivers, tracking, calculations, alerting, and persistence.

    ``supervisor`` (:class:`ReceiverSupervisor`) owns the receiver threads
    and intake queue, ``archiver`` (:class:`FlightArchiver`) the archive
    and its recovery paths; this class runs the tick loop over them.

    *history_store* replaces the archive writer built from the config, e.g.
    a real archive behind an ``isolated`` (memory-only) live store.

    Test hooks: :meth:`tick` runs one iteration, :meth:`process_frames` its
    frame stages, :meth:`expire_planes` its expiry; :meth:`request_stop` is
    what a signal does. ``supervisor.enqueue(frame)`` feeds frames as a
    receiver would.
    """

    def __init__(
        self,
        config: Config,
        *,
        aircraft_db_path: str = DEFAULT_AIRCRAFT_DB,
        isolated: bool = False,
        history_store: ArchiveWriter | None = None,
    ):
        self.config = config
        self._isolated = isolated
        self.positions = ReceiverPositions(config, fetch=not isolated)
        self.tracker = Tracker(config, positions=self.positions)
        self.integrity = IntegrityMonitor(config, self.positions)
        self.polygons: dict[str, Polygon] = build_polygons(config.zones)
        self.aircraft_db = AircraftDB(aircraft_db_path)
        self.live_store: LiveWriteStore = LiveWriter(
            config.database.redis_uri,
            memory_only=isolated,
            telemetry_keep_seconds=config.tracking.telemetry_keep_seconds,
            writer=True,
        )
        self._last_status_log = 0.0
        self.history_store = (
            ArchiveWriter(config.database.path, disabled=isolated)
            if history_store is None
            else history_store
        )
        # Alert episodes go to the archive (the record) and the live cache.
        self.episodes = EpisodeRecorder(self.history_store, self.live_store)
        self.calculator = PlaneCalculator(
            config, self.polygons, self.aircraft_db, self.episodes
        )
        self._running = False
        self._shutdown = threading.Event()
        self._shutdown_done = False
        # True once another engine took the live writer: shutdown then leaves
        # Redis and the in-flight archive to it.
        self.yielded_writer = False
        self._guard = StageGuard()
        self._previous_handlers: dict[int, _SignalHandler] = {}
        self._processed_since_status = 0
        # Cumulative since start, published in the heartbeat (`counters`).
        self._started_at = time.time()
        self._messages_processed = 0
        self.supervisor = ReceiverSupervisor(
            config,
            self._guard,
            self._shutdown,
            archive=self.history_store,
            publisher=self.live_store,
            dedupe=self.tracker,
        )
        self.archiver = FlightArchiver(
            config,
            self.polygons,
            self._guard,
            archive=self.history_store,
            live=self.live_store,
            alerts=self.calculator,
            tracked=lambda: self.tracker.planes,
            episodes=self.episodes,
        )
        if not isolated:
            if not self.live_store.ping():
                raise RuntimeError(
                    "Redis live store is unreachable. Start Redis and check "
                    "database.redis_uri before running `pyaerial run`."
                )
            if not self.live_store.claim_engine():
                raise RuntimeError(
                    "Another tracking engine is already running (fresh live Redis "
                    "heartbeat). Stop the other `pyaerial run` process first."
                )
            self.archiver.recover_on_startup()

    def start_receivers(self) -> None:
        self.supervisor.start()

    def run(self) -> None:
        """Blocking main loop."""
        self._running = True
        self._install_signal_handlers()
        hz = self.config.tracking.hz
        tick_budget = 1.0 / hz
        store = "memory" if self._isolated else "redis+sqlite"

        # Startup failures (e.g. no receiver could start) still shut down, so
        # the heartbeat claimed in __init__ is released and stores are closed.
        try:
            self.positions.start()
            self.integrity.start()
            self.start_receivers()
            log.info(
                "PyAerial running at up to %.1f Hz with %d receiver(s), store=%s",
                hz,
                len(self.supervisor.receivers),
                store,
            )
            self._publish_health()
            self.live_store.touch_engine()
            log.info("Receivers available: %s", available_receivers())
            log.info("Alerters available: %s", available_alerters())

            while self._running and not self._shutdown.is_set():
                start = time.monotonic()
                writer = True
                with self._guard("writer check"):
                    writer = self._isolated or self.live_store.ensure_writer()
                if not writer:
                    log.error(
                        "Another tracking engine holds live Redis; yielding writer"
                    )
                    self.yielded_writer = True
                    break
                processed = self.tick()
                with self._guard("status"):
                    self.log_status(processed)

                elapsed = time.monotonic() - start
                sleep_for = tick_budget - elapsed
                if sleep_for > 0:
                    if self._shutdown.wait(sleep_for):
                        break
                else:
                    log.warning(
                        "Main loop behind by %.2fs (%.2fs/%.2fs budget)",
                        -sleep_for,
                        elapsed,
                        tick_budget,
                    )
        except KeyboardInterrupt:
            log.info("Keyboard interrupt received")
        finally:
            self.shutdown()
            self._restore_signal_handlers()

    def tick(self) -> int:
        """Run one pass of the pipeline. Returns the number of messages ingested.

        Each stage is guarded on its own: a bug or outage in one (integrity,
        alerting, Redis, SQLite) is logged and the others keep running, rather
        than the exception ending ``run()`` and all tracking with it.
        """
        guard = self._guard
        with guard("receiver restart"):
            self.supervisor.restart_dead()

        processed = self.process_frames(self.supervisor.drain())

        with guard("integrity"):
            self.integrity.evaluate(self.tracker.planes)
        with guard("calculations"):
            self.calculator.calculate_all(self.tracker.planes)
        with guard("live store"):
            self.live_store.write_live_planes(self.tracker.planes)
        with guard("heartbeat"):
            self._publish_health()
            self.live_store.touch_engine()

        with guard("expiry"):
            self.expire_planes(time.time())
        with guard("segments"):
            self.archiver.split_long_flights()
        self.archiver.tick()
        with guard("pending pops"):
            self.live_store.retry_pending_pops()
        return processed

    def process_frames(self, raw: list[RawFrame]) -> int:
        """Integrity, raw archive, merge, raw feed, and tracking for one batch."""
        guard = self._guard
        supervisor = self.supervisor
        # Integrity sees every copy before merge/de-dup: per-receiver
        # copies drive TDOA, exact repeats reveal replay.
        with guard("integrity observe"):
            self.integrity.observe_frames(raw)
        with guard("raw frame archive"):
            supervisor.archive_frames(raw)
        outgoing: list[RawFrame] = []
        new_messages: list[tuple[str, float]] = []
        heard_by: dict[str, tuple[str, ...]] = {}
        with guard("merge"):
            outgoing, new_messages, heard_by = supervisor.merge(raw)
        with guard("raw publish"):
            supervisor.publish_raw(outgoing, heard_by)
        processed = 0
        with guard("tracking"):
            processed = self.tracker.ingest(new_messages, receivers=heard_by)
        self._messages_processed += processed
        with guard("track start"):
            self._mark_track_started()
        return processed

    def _publish_health(self) -> None:
        """History health for the ``live:engine`` heartbeat (compose healthcheck)."""
        self.live_store.set_engine_health(
            **self.archiver.health(),
            counters=self._counters(),
        )

    def _mark_track_started(self) -> None:
        """Record when the engine first tracked each aircraft.

        Kept across segment splits, which move ``first_packet``, so checks
        that need a track's real age (integrity) can read it.
        """
        for plane in self.tracker.planes.values():
            track = plane.track
            if track.track_started is None:
                track.track_started = track.first_packet

    def expire_planes(self, now: float) -> None:
        """Finalize planes not heard from within ``tracking.remember_planes``."""
        expired = self.tracker.expired_planes(now)
        if not expired:
            return
        removed = self.tracker.remove_planes(expired)
        for plane in removed:
            with self._guard("finalize"):
                self.archiver.finalize(plane)
        with self._guard("integrity forget"):
            for icao in expired:
                self.integrity.forget(icao)
        log.debug("Expired %d plane(s): %s", len(expired), expired)

    def log_status(self, processed: int, *, now: float | None = None) -> None:
        """INFO status every status_interval, counting messages since the last.

        *now* is ``time.monotonic()`` unless given.
        """
        self._processed_since_status += processed
        now_mono = time.monotonic() if now is None else now
        if now_mono - self._last_status_log < self.config.tracking.status_interval:
            if log.isEnabledFor(logging.DEBUG):
                log.debug(
                    "Tick: processed %d msg(s), tracking %d plane(s)",
                    processed,
                    len(self.tracker.planes),
                )
            return
        summary = self.tracker.top_planes_summary()
        total = self._processed_since_status
        status = f"processed {total} msg(s). " if total else ""
        log.info(
            "%sTracking %d plane(s). %s", status, len(self.tracker.planes), summary
        )
        self._last_status_log = now_mono
        self._processed_since_status = 0

    def _counters(self) -> dict[str, object]:
        """Heartbeat ``counters``: cumulative since ``since`` (process start).

        ``planes_tracked`` is the current count. ``frames_received`` counts
        frames taken off the intake queue; ``frames_dropped`` those discarded
        because it was full (together: everything the receivers delivered).
        """
        intake = self.supervisor.counters()
        return {
            "since": self._started_at,
            "frames_received": intake["frames_received"],
            "frames_dropped": intake["frames_dropped"],
            "messages_processed": self._messages_processed,
            "planes_tracked": len(self.tracker.planes),
            "pending_evicted": self.archiver.counters()["pending_evicted"],
            "receiver_restarts": intake["receiver_restarts"],
            "stage_errors": self._guard.counts(),
        }

    def request_stop(self) -> None:
        """Ask ``run()`` to leave its loop and shut down (what a signal does)."""
        self._running = False
        self._shutdown.set()

    @property
    def stop_requested(self) -> bool:
        """A signal, :meth:`request_stop` or :meth:`shutdown` asked ``run()`` to end."""
        return self._shutdown.is_set()

    def shutdown(self) -> None:
        """Stop receivers and finalize any remaining live flights.

        Every step is guarded, so one failure cannot skip archiving the other
        flights or closing the stores.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._running = False
        self._shutdown.set()
        log.info("Shutting down...")

        with self._guard("stop positions"):
            self.positions.stop()
        with self._guard("stop integrity"):
            self.integrity.stop()
        self.supervisor.stop()

        if not self.yielded_writer:
            # Held and still-queued frames belong to the flights archived now.
            with self._guard("flush frames"):
                self.process_frames(self.supervisor.flush())

        if self.yielded_writer:
            log.info(
                "Yielded live writer; leaving Redis and in-flight archive "
                "to the other engine"
            )
            self.tracker.planes.clear()
        else:
            for plane in list(self.tracker.planes.values()):
                with self._guard("finalize"):
                    self.archiver.finalize(plane)
            self.tracker.planes.clear()
            self.archiver.shutdown()
            with self._guard("pending pops"):
                self.live_store.retry_pending_pops()
            with self._guard("heartbeat"):
                self.live_store.clear_engine()

        for name, close in (
            ("calculator", self.calculator.close),
            ("live store", self.live_store.close),
            ("history store", self.history_store.close),
            ("aircraft db", self.aircraft_db.close),
        ):
            with self._guard(f"close {name}"):
                close()
        log.info("Shutdown complete")

    def _install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            # The first signal asks for a graceful shutdown (alerts closed,
            # flights archived). Restoring the OS default lets a second one
            # kill a shutdown that hangs (slow disk, stuck alert webhook).
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, signal.SIG_DFL)
            log.warning(
                "Received signal %s; shutting down (send it again to force exit)",
                signum,
            )
            self.request_stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            # Not the main thread (tests, embedding): keep default handling.
            with contextlib.suppress(ValueError, OSError):
                previous = signal.signal(sig, _handler)
                self._previous_handlers.setdefault(sig, previous)

    def _restore_signal_handlers(self) -> None:
        """Put back the handlers run() replaced (embedding, tests)."""
        for sig, previous in self._previous_handlers.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, previous)
        self._previous_handlers.clear()


def run_engine(config: Config, *, aircraft_db_path: str = DEFAULT_AIRCRAFT_DB) -> None:
    """Initialize logging and execute engine event loop until termination signal."""
    setup_logging(config.logging.level, log_file=config.logging.file)
    Engine(config, aircraft_db_path=aircraft_db_path).run()
