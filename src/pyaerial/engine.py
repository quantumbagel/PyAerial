"""
PyAerial main engine: receiver management, main loop, and graceful shutdown.
"""

from __future__ import annotations

import logging
import queue
import signal
import threading
import time
from dataclasses import dataclass

from shapely import Polygon

from pyaerial.alerters import available_alerters
from pyaerial.calc.aircraft_db import AircraftDB
from pyaerial.calc.geo import build_polygons
from pyaerial.calc.plane import PlaneCalculator
from pyaerial.config.schema import Config
from pyaerial.constants import DEFAULT_AIRCRAFT_DB
from pyaerial.logging_setup import setup_logging
from pyaerial.receivers import Receiver, available_receivers, create_receiver
from pyaerial.store import MongoStore, RedisLiveStore, flight_id_for_plane
from pyaerial.tracker import Tracker

log = logging.getLogger("pyaerial.engine")

_RECEIVER_BACKOFF_INITIAL = 1.0
_RECEIVER_BACKOFF_MAX = 30.0
_RECEIVER_BACKOFF_RESET_AFTER = 10.0
_MESSAGE_QUEUE_MAXSIZE = 10_000
_PENDING_FINALIZE_MAX = 256
_DROP_LOG_INTERVAL = 10.0


@dataclass
class _ReceiverHandle:
    name: str
    method: str
    receiver: Receiver
    thread: threading.Thread
    last_error: str = ""
    backoff: float = _RECEIVER_BACKOFF_INITIAL
    next_restart_at: float = 0.0
    started_at: float = 0.0


class Engine:
    """Orchestrates receivers, tracking, calculations, alerting, and persistence."""

    def __init__(
        self,
        config: Config,
        *,
        aircraft_db_path: str = DEFAULT_AIRCRAFT_DB,
        isolated: bool = False,
    ):
        self.config = config
        self.tracker = Tracker(config)
        self.polygons: dict[str, Polygon] = build_polygons(config.zones)
        self.aircraft_db = AircraftDB(aircraft_db_path)
        self.live_store = RedisLiveStore(
            config.database.redis_uri,
            memory_only=isolated,
            telemetry_keep_seconds=config.tracking.telemetry_keep_seconds,
        )
        self._last_status_log = 0.0
        if not isolated:
            self.live_store.clear_all()
        self.mongo_store = MongoStore(config, self.polygons, disabled=isolated)
        self.calculator = PlaneCalculator(
            config, self.polygons, self.aircraft_db, self.live_store
        )
        self._message_queue: queue.Queue[tuple[str, float, str]] = queue.Queue(
            maxsize=_MESSAGE_QUEUE_MAXSIZE
        )
        self._receivers: dict[str, _ReceiverHandle] = {}
        self._running = False
        self._shutdown = threading.Event()
        self._shutdown_done = False
        self._pending_finalize: dict[str, dict] = {}
        self._dropped_messages = 0
        self._last_drop_log = 0.0

    def start_receivers(self) -> None:
        for name, receiver_cfg in self.config.receivers.items():
            self._start_receiver(
                name, receiver_cfg.type, receiver_cfg.receiver_arguments()
            )

        if not self._receivers:
            raise RuntimeError("no receivers could be started")

    def _start_receiver(
        self,
        name: str,
        method: str,
        arguments: dict,
        *,
        backoff: float = _RECEIVER_BACKOFF_INITIAL,
    ) -> None:
        def emit(msg_hex: str, timestamp: float, receiver_name: str = name) -> None:
            self._enqueue_message(msg_hex, timestamp, receiver_name)

        try:
            receiver = create_receiver(method, name, emit, arguments)
        except KeyError as exc:
            log.error("Receiver %s: %s", name, exc)
            return

        thread = threading.Thread(
            target=self._run_receiver,
            args=(name, receiver),
            name=f"receiver-{name}",
            daemon=True,
        )
        self._receivers[name] = _ReceiverHandle(
            name,
            method,
            receiver,
            thread,
            backoff=backoff,
            started_at=time.monotonic(),
        )
        thread.start()
        log.info("Started receiver %r (%s)", name, method)

    def _enqueue_message(
        self, msg_hex: str, timestamp: float, receiver_name: str
    ) -> None:
        """Push a raw frame onto the bounded queue, dropping the oldest if full."""
        item = (msg_hex, timestamp, receiver_name)
        try:
            self._message_queue.put_nowait(item)
            return
        except queue.Full:
            pass
        dropped = 0
        try:
            self._message_queue.get_nowait()
            dropped += 1
        except queue.Empty:
            pass
        try:
            self._message_queue.put_nowait(item)
        except queue.Full:
            dropped += 1
        if dropped:
            self._note_dropped(dropped)

    def _note_dropped(self, count: int) -> None:
        self._dropped_messages += count
        now = time.monotonic()
        if now - self._last_drop_log < _DROP_LOG_INTERVAL:
            return
        log.warning(
            "Message queue full (max %d); dropped %d frame(s) since last report",
            _MESSAGE_QUEUE_MAXSIZE,
            self._dropped_messages,
        )
        self._dropped_messages = 0
        self._last_drop_log = now

    def _run_receiver(self, name: str, receiver: Receiver) -> None:
        try:
            reason = receiver.run()
        except Exception as exc:  # pragma: no cover - receiver crash guard
            reason = f"unhandled exception: {exc}"
            log.exception("Receiver %s crashed", name)
        else:
            if reason:
                log.warning("Receiver %s stopped: %s", name, reason)
        handle = self._receivers.get(name)
        if handle is not None:
            handle.last_error = reason or ""

    def _restart_dead_receivers(self) -> None:
        now = time.monotonic()
        for name, handle in list(self._receivers.items()):
            if handle.thread.is_alive():
                continue
            if self._shutdown.is_set():
                return
            if handle.next_restart_at <= 0.0:
                if now - handle.started_at >= _RECEIVER_BACKOFF_RESET_AFTER:
                    handle.backoff = _RECEIVER_BACKOFF_INITIAL
                handle.next_restart_at = now + handle.backoff
                log.warning(
                    "Receiver %r (%s) stopped%s; retry in %.1fs",
                    name,
                    handle.method,
                    f": {handle.last_error}" if handle.last_error else "",
                    handle.backoff,
                )
                handle.backoff = min(handle.backoff * 2.0, _RECEIVER_BACKOFF_MAX)
                continue
            if now < handle.next_restart_at:
                continue
            log.warning("Restarting receiver %r (%s)", name, handle.method)
            handle.receiver.stop()
            cfg = self.config.receivers[name]
            next_backoff = handle.backoff
            self._receivers.pop(name, None)
            self._start_receiver(
                name, cfg.type, cfg.receiver_arguments(), backoff=next_backoff
            )

    def _drain_messages(self) -> list[tuple[str, float, str]]:
        batch: list[tuple[str, float, str]] = []
        while True:
            try:
                msg_hex, timestamp, receiver = self._message_queue.get_nowait()
            except queue.Empty:
                break
            batch.append((msg_hex, timestamp, receiver))
        return batch

    def run(self) -> None:
        """Blocking main loop."""
        self._running = True
        self._install_signal_handlers()
        self.start_receivers()

        hz = self.config.tracking.hz
        tick_budget = 1.0 / hz
        log.info(
            "PyAerial running at up to %.1f Hz with %d receiver(s), store=redis+mongodb",
            hz,
            len(self._receivers),
        )
        log.info("Receivers available: %s", available_receivers())
        log.info("Alerters available: %s", available_alerters())

        try:
            while self._running and not self._shutdown.is_set():
                start = time.time()
                self._restart_dead_receivers()

                raw = self._drain_messages()
                pairs = [(hex_msg, ts) for hex_msg, ts, _recv in raw]
                receivers = {hex_msg: recv for hex_msg, _ts, recv in raw if recv}
                new_messages = self.tracker.collect_new_messages(pairs)
                processed = self.tracker.ingest(new_messages, receivers=receivers)

                self.calculator.calculate_all(self.tracker.planes)
                self.live_store.write_live_planes(self.tracker.planes)

                now = time.time()
                expired = self.tracker.expired_planes(now)
                if expired:
                    removed = self.tracker.remove_planes(expired)
                    for plane in removed:
                        self._finalize_plane(plane)
                    log.debug("Expired %d plane(s): %s", len(expired), expired)
                self._retry_pending_finalizes()

                summary = self.tracker.top_planes_summary()
                status = f"processed {processed} msg(s). " if processed else ""
                status_line = (
                    f"{status}Tracking {len(self.tracker.planes)} plane(s). {summary}"
                )
                now_mono = time.monotonic()
                if now_mono - self._last_status_log >= self.config.tracking.status_interval:
                    log.info("%s", status_line)
                    self._last_status_log = now_mono
                else:
                    log.debug("%s", status_line)

                elapsed = time.time() - start
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

    def shutdown(self) -> None:
        """Stop receivers and finalize any remaining live flights."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._running = False
        self._shutdown.set()
        log.info("Shutting down...")

        for handle in self._receivers.values():
            handle.receiver.stop()
        for handle in self._receivers.values():
            handle.thread.join(timeout=2.0)

        for plane in list(self.tracker.planes.values()):
            self._finalize_plane(plane)
        self.tracker.planes.clear()
        self._retry_pending_finalizes()

        self.calculator.close()
        self.live_store.close()
        self.mongo_store.close()
        self.aircraft_db.close()
        log.info("Shutdown complete")

    def _finalize_plane(self, plane: dict) -> None:
        """Deactivate alerts, persist to Mongo, then drop the live Redis copy.

        Redis is only popped after Mongo accepts the write (or the flight is
        intentionally not retained) so a Mongo outage cannot lose history.
        """
        self.calculator.deactivate_plane(plane)
        flight_id = flight_id_for_plane(plane)
        alerts = self.live_store.get_alerts(flight_id=flight_id, active_only=False)
        if self.mongo_store.finalize_plane(plane, alerts=alerts):
            self._pending_finalize.pop(flight_id, None)
            self.live_store.pop_flight(flight_id)
        else:
            log.warning(
                "Deferred persist for %s; keeping live data until Mongo is back",
                flight_id,
            )
            self._enqueue_pending_finalize(flight_id, plane)

    def _enqueue_pending_finalize(self, flight_id: str, plane: dict) -> None:
        if flight_id in self._pending_finalize:
            self._pending_finalize[flight_id] = plane
            return
        while len(self._pending_finalize) >= _PENDING_FINALIZE_MAX:
            dropped_id, _dropped = next(iter(self._pending_finalize.items()))
            del self._pending_finalize[dropped_id]
            self.live_store.pop_flight(dropped_id)
            log.error(
                "Pending finalize cap (%d) reached; dropped retry for %s "
                "(Mongo still down; live copy removed)",
                _PENDING_FINALIZE_MAX,
                dropped_id,
            )
        self._pending_finalize[flight_id] = plane

    def _retry_pending_finalizes(self) -> None:
        if not self._pending_finalize:
            return
        still: dict[str, dict] = {}
        for flight_id, plane in self._pending_finalize.items():
            alerts = self.live_store.get_alerts(flight_id=flight_id, active_only=False)
            if self.mongo_store.finalize_plane(plane, alerts=alerts):
                self.live_store.pop_flight(flight_id)
            else:
                still[flight_id] = plane
        self._pending_finalize = still

    def _install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            log.info("Received signal %s", signum)
            self._running = False
            self._shutdown.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass


def run_engine(config: Config, *, aircraft_db_path: str = DEFAULT_AIRCRAFT_DB) -> None:
    """Configure logging and run the engine until shutdown."""
    setup_logging(config.logging.level, log_file=config.logging.file)
    Engine(config, aircraft_db_path=aircraft_db_path).run()
