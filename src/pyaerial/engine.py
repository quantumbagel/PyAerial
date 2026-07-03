"""
PyAerial main engine: receiver management, main loop, and graceful shutdown.
"""
from __future__ import annotations

import logging
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass, field

from shapely import Polygon

from pyaerial.alerters import available_alerters
from pyaerial.calc.aircraft_db import AircraftDB
from pyaerial.calc.geo import build_polygons
from pyaerial.calc.plane import PlaneCalculator
from pyaerial.config.schema import Config
from pyaerial.constants import DEFAULT_AIRCRAFT_DB
from pyaerial.logging_setup import setup_logging
from pyaerial.receivers import Receiver, available_receivers, create_receiver
from pyaerial.savers import Saver, available_savers, create_saver
from pyaerial.tracker import Tracker

log = logging.getLogger("pyaerial.engine")


@dataclass
class _ReceiverHandle:
    name: str
    method: str
    receiver: Receiver
    thread: threading.Thread
    last_error: str = ""


class Engine:
    """Orchestrates receivers, tracking, calculations, alerting, and persistence."""

    def __init__(self, config: Config, *, aircraft_db_path: str = DEFAULT_AIRCRAFT_DB):
        self.config = config
        self.tracker = Tracker(config)
        self.polygons: dict[str, Polygon] = build_polygons(config.zones)
        self.aircraft_db = AircraftDB(aircraft_db_path)
        self.calculator = PlaneCalculator(config, self.polygons, self.aircraft_db)
        self.saver: Saver = create_saver(config.general.saver, config, self.polygons)
        self._message_queue: queue.Queue[tuple[str, float, str]] = queue.Queue()
        self._receivers: dict[str, _ReceiverHandle] = {}
        self._running = False
        self._shutdown = threading.Event()

    def start_receivers(self) -> None:
        for name, receiver_cfg in self.config.receivers.items():
            self._start_receiver(name, receiver_cfg.method, receiver_cfg.arguments)

        if not self._receivers:
            raise RuntimeError("no receivers could be started")

    def _start_receiver(self, name: str, method: str, arguments: dict) -> None:
        def emit(msg_hex: str, timestamp: float, receiver_name: str = name) -> None:
            self._message_queue.put((msg_hex, timestamp, receiver_name))

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
        self._receivers[name] = _ReceiverHandle(name, method, receiver, thread)
        thread.start()
        log.info("Started receiver %r (%s)", name, method)

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
        for name, handle in list(self._receivers.items()):
            if handle.thread.is_alive():
                continue
            if self._shutdown.is_set():
                return
            log.warning("Restarting receiver %r (%s)%s", name, handle.method,
                        f": {handle.last_error}" if handle.last_error else "")
            handle.receiver.stop()
            cfg = self.config.receivers[name]
            self._receivers.pop(name, None)
            self._start_receiver(name, cfg.method, cfg.arguments)

    def _drain_messages(self) -> list[tuple[str, float]]:
        batch: list[tuple[str, float]] = []
        while True:
            try:
                msg_hex, timestamp, _receiver = self._message_queue.get_nowait()
            except queue.Empty:
                break
            batch.append((msg_hex, timestamp))
        return batch

    def run(self) -> None:
        """Blocking main loop."""
        self._running = True
        self._install_signal_handlers()
        self.start_receivers()

        hz = self.config.general.hz
        tick_budget = 1.0 / hz
        log.info(
            "PyAerial running at up to %.1f Hz with %d receiver(s), saver=%s",
            hz, len(self._receivers), self.config.general.saver,
        )
        log.info("Receivers available: %s", available_receivers())
        log.info("Savers available: %s", available_savers())
        log.info("Alerters available: %s", available_alerters())

        try:
            while self._running and not self._shutdown.is_set():
                start = time.time()
                self._restart_dead_receivers()

                raw = self._drain_messages()
                new_messages = self.tracker.collect_new_messages(raw)
                processed = self.tracker.ingest(new_messages)

                self.calculator.calculate_all(self.tracker.planes)

                now = time.time()
                expired = self.tracker.expired_planes(now)
                if expired:
                    removed = self.tracker.remove_planes(expired)
                    should_save = False
                    for plane in removed:
                        if self.saver.cache_flight(plane):
                            should_save = True
                    if should_save:
                        self.saver.save()
                    log.debug("Expired %d plane(s): %s", len(expired), expired)

                summary = self.tracker.top_planes_summary()
                status = f"processed {processed} msg(s). " if processed else ""
                log.info("%sTracking %d plane(s). %s", status,
                         len(self.tracker.planes), summary)

                elapsed = time.time() - start
                sleep_for = tick_budget - elapsed
                if sleep_for > 0:
                    if self._shutdown.wait(sleep_for):
                        break
                else:
                    log.warning(
                        "Main loop behind by %.2fs (%.2fs/%.2fs budget)",
                        -sleep_for, elapsed, tick_budget,
                    )
        except KeyboardInterrupt:
            log.info("Keyboard interrupt received")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Stop receivers and flush any pending saves."""
        if not self._running and self._shutdown.is_set():
            return
        self._running = False
        self._shutdown.set()
        log.info("Shutting down...")

        for handle in self._receivers.values():
            handle.receiver.stop()
        for handle in self._receivers.values():
            handle.thread.join(timeout=2.0)

        if self.saver._cache:
            log.info("Flushing %d cached flight-level(s) on shutdown", len(self.saver._cache))
            self.saver.save()

        self.calculator.close()
        self.saver.close()
        self.aircraft_db.close()
        log.info("Shutdown complete")

    def _install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            log.info("Received signal %s", signum)
            self._running = False
            self._shutdown.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):  # not available on all platforms/threads
                pass


def run_engine(config: Config, *, aircraft_db_path: str = DEFAULT_AIRCRAFT_DB) -> None:
    """Configure logging and run the engine until shutdown."""
    setup_logging(config.general.logs, log_file=config.general.log_file)
    Engine(config, aircraft_db_path=aircraft_db_path).run()
