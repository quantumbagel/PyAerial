"""Tracking engine managing receiver threads, frame ingestion, and zone alerting."""

from __future__ import annotations

import contextlib
import logging
import math
import queue
import signal
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

from shapely import Polygon

from pyaerial.alerters import available_alerters
from pyaerial.alerts.engine import ENDED_RESTART
from pyaerial.alerts.retain import should_retain
from pyaerial.calc.geo import build_polygons
from pyaerial.config.schema import Config
from pyaerial.constants import (
    DEFAULT_AIRCRAFT_DB,
    STORE_CALC_DATA,
    STORE_CALLSIGN,
    STORE_FIRST_PACKET,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTEGRITY_LOG,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_MOST_RECENT_PACKET,
    STORE_POSITION_RECEIVERS,
    STORE_RECV_DATA,
    STORE_TRACK_STARTED,
)
from pyaerial.enrich.aircraft_db import AircraftDB
from pyaerial.integrity import IntegrityMonitor
from pyaerial.logging_setup import setup_logging
from pyaerial.models import Datum, flight_id_for_plane
from pyaerial.pipeline import PlaneCalculator
from pyaerial.receivers import RawFrame, Receiver, available_receivers, create_receiver
from pyaerial.receivers.frames import es_crc_ok, merge_receiver_frames, raw_payload
from pyaerial.receivers.positions import ReceiverPositions
from pyaerial.store import HistoryStore, RedisLiveStore
from pyaerial.store.history import FRAME_PRUNE_BATCH
from pyaerial.store.redis_live import LiveUnavailable
from pyaerial.tracker import Tracker

log = logging.getLogger("pyaerial.engine")

_RECEIVER_BACKOFF_INITIAL = 1.0
_RECEIVER_BACKOFF_MAX = 30.0
_RECEIVER_BACKOFF_RESET_AFTER = 10.0
# Receiver threads -> main loop queue, drained once per tick. It must hold a
# tick's frames plus the backlog of a slow tick or two, or drop-oldest throws
# away data the engine would have caught up on: a fixed 10k capped intake at
# 20k frames/s at hz=2, and a 900-aircraft, 4-receiver profile (~20.5k
# frames/s) dropped 2.7%. Sized for 3 ticks at a generous 30k frames/s
# peak (45k at hz=2). Floor: 1 s of peak for fast ticks, since one GC pause or
# slow archive write spans several of them. Ceiling: queued frames measure
# ~250 B each, so 240k (8 s of peak) is ~60 MB; beyond that the engine is
# not keeping up and dropping is the right answer.
_PEAK_FRAMES_PER_SECOND = 30_000
_MESSAGE_QUEUE_TICKS = 3
_MESSAGE_QUEUE_MIN = _PEAK_FRAMES_PER_SECOND
_MESSAGE_QUEUE_MAX = 8 * _PEAK_FRAMES_PER_SECOND
# Memory held by flights waiting for the archive (history down). Tracker-built
# planes keep the whole flight, measured at ~770 B per position sample (about
# 100 B per stored value), so a 30 min flight is ~2.8 MB. A count cap is
# neither a memory bound nor a loss bound: 256 long flights is gigabytes, 256
# short ones a few MB. _pending_cost runs ~30% above measured, so real use
# stays under the budget; 1 GiB holds ~330 30 min flights (~160 flight-hours,
# ~2000 10 min ones). Beyond it the oldest fall back to their live Redis copy.
# Spilling to disk was rejected: the likeliest cause of a long outage is a
# full disk (raw_frames writes GBs a day to the same volume), where a spool
# fails too, and it would add a second recovery path.
_PENDING_FINALIZE_BUDGET_BYTES = 1 << 30
_PENDING_BYTES_PER_VALUE = 100
_PENDING_BYTES_PER_PLANE = 4_096
_DROP_LOG_INTERVAL = 10.0
_RAW_PUBLISH_BATCH = 2_000
_FRAME_PRUNE_INTERVAL = 600.0
# A failing stage logs its traceback at most this often (seconds).
_STAGE_ERROR_LOG_INTERVAL = 60.0
# After an archive write fails, wait this long before retrying deferred flights.
_HISTORY_RETRY_INTERVAL = 10.0
# Per-tick time for archiving deferred and orphaned flights once history is
# back: ~25 ms per 30 min flight measured, so ~8 flights a tick. Catching up
# on hundreds in one tick took ~8 s, close to the 10 s heartbeat TTL.
# Shutdown's final attempt is not capped.
_ARCHIVE_TICK_BUDGET = 0.2
# Refresh the heartbeat this often while archiving leftovers at startup.
_STARTUP_HEARTBEAT_INTERVAL = 1.0
# Receiver copies of one transmission land within this many seconds of each
# other; merge_receiver_frames uses the same window. Frames this young wait
# for the next tick so a late copy (another receiver, the next TCP chunk) is
# merged with the rest instead of being dropped as a duplicate.
_MERGE_WINDOW = 0.1
# tracking.max_flight_seconds default: longer tracks (ground vehicles, parked
# aircraft) are archived in segments instead of growing in memory forever.
_DEFAULT_MAX_FLIGHT_SECONDS = 14_400.0


def _pending_cost(plane: dict) -> int:
    """Approximate bytes held by an in-memory plane (O(number of series))."""
    values = 0
    for bucket in (STORE_RECV_DATA, STORE_CALC_DATA):
        for series in (plane.get(bucket) or {}).values():
            values += len(series)
    internal = plane.get(STORE_INTERNAL) or {}
    values += len(internal.get(STORE_POSITION_RECEIVERS) or ())
    # A verdict carries the full check results: count it as ~10 values.
    values += 10 * len(internal.get(STORE_INTEGRITY_LOG) or ())
    return _PENDING_BYTES_PER_PLANE + values * _PENDING_BYTES_PER_VALUE


def _message_queue_size(hz: float) -> int:
    """Frames the intake queue holds: ~3 ticks at peak rate, within bounds."""
    wanted = math.ceil(_PEAK_FRAMES_PER_SECOND * _MESSAGE_QUEUE_TICKS / hz)
    return max(_MESSAGE_QUEUE_MIN, min(_MESSAGE_QUEUE_MAX, wanted))


def _plane_for_stray_alerts(flight_id: str, docs: list[dict]) -> dict | None:
    """Minimal plane for alerts whose flight never reached ``live:flights``.

    Rebuilt from the alert docs alone: ICAO and first packet come from the
    flight id (``{icao}-{first_packet}``) so ``flight_id_for_plane`` matches,
    the position from the last alert update.
    """
    icao, _, first_text = flight_id.rpartition("-")
    try:
        first = float(int(first_text))
    except ValueError:
        return None
    if not icao:
        return None
    stamps = [
        value
        for doc in docs
        for value in (doc.get("activated_at"), doc.get("last_updated"))
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    last = max([first, *stamps])
    plane: dict = {
        STORE_INFO: {
            STORE_ICAO: icao,
            STORE_CALLSIGN: next(
                (d["callsign"] for d in docs if d.get("callsign")), ""
            ),
        },
        STORE_INTERNAL: {STORE_FIRST_PACKET: first, STORE_MOST_RECENT_PACKET: last},
        STORE_RECV_DATA: {},
    }
    newest = max(
        docs, key=lambda d: d.get("last_updated") or d.get("activated_at") or 0
    )
    coords = (newest.get("position") or {}).get("coordinates") or []
    if len(coords) >= 2 and all(
        isinstance(c, (int, float)) and math.isfinite(c) for c in coords[:2]
    ):
        plane[STORE_RECV_DATA] = {
            STORE_LAT: [Datum(coords[1], last)],
            STORE_LONG: [Datum(coords[0], last)],
        }
    return plane


def _position_samples(plane: dict) -> int:
    """Telemetry samples the archive would write for *plane*.

    Equals ``len(build_telemetry_docs(plane, ...))`` without building them:
    one sample per latitude or longitude timestamp, when both series exist
    (see :func:`pyaerial.models.iter_telemetry_samples`).
    """
    recv = plane.get(STORE_RECV_DATA) or {}
    lat, lon = recv.get(STORE_LAT) or [], recv.get(STORE_LONG) or []
    if not lat or not lon:
        return 0
    return len({datum.time for datum in lat} | {datum.time for datum in lon})


def _merge_key(hex_msg: str) -> tuple[int, str]:
    """Coarse transmission identity: length plus DF/CA and address bytes."""
    return len(hex_msg), hex_msg[:8]


def _segment_copy(plane: dict) -> dict:
    """A detached copy of a plane's series for archiving one segment."""
    internal = dict(plane.get(STORE_INTERNAL) or {})
    internal[STORE_POSITION_RECEIVERS] = dict(
        internal.get(STORE_POSITION_RECEIVERS) or {}
    )
    internal[STORE_INTEGRITY_LOG] = list(internal.get(STORE_INTEGRITY_LOG) or [])
    return {
        STORE_INFO: dict(plane.get(STORE_INFO) or {}),
        STORE_RECV_DATA: {
            k: list(v) for k, v in (plane.get(STORE_RECV_DATA) or {}).items()
        },
        STORE_CALC_DATA: {
            k: list(v) for k, v in (plane.get(STORE_CALC_DATA) or {}).items()
        },
        STORE_INTERNAL: internal,
        "active_alerts": list(plane.get("active_alerts") or []),
    }


def _closed_alert(alert: dict) -> dict:
    """Mark active episode closed with a deactivation timestamp."""
    doc = dict(alert)
    if doc.get("active") and not doc.get("deactivated_at"):
        doc["active"] = False
        doc["deactivated_at"] = time.time()
    return doc


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
        self._isolated = isolated
        self.positions = ReceiverPositions(config, fetch=not isolated)
        self.tracker = Tracker(config, positions=self.positions)
        self.integrity = IntegrityMonitor(config, self.positions)
        self.polygons: dict[str, Polygon] = build_polygons(config.zones)
        self.aircraft_db = AircraftDB(aircraft_db_path)
        self.live_store = RedisLiveStore(
            config.database.redis_uri,
            memory_only=isolated,
            telemetry_keep_seconds=config.tracking.telemetry_keep_seconds,
            writer=True,
        )
        self._last_status_log = 0.0
        # -inf, not 0: monotonic() can be under the interval on a fresh boot.
        self._last_frame_prune = float("-inf")
        self.history_store = HistoryStore(config.database.path, disabled=isolated)
        self.calculator = PlaneCalculator(
            config, self.polygons, self.aircraft_db, self.live_store
        )
        self._message_queue: queue.Queue[RawFrame] = queue.Queue(
            maxsize=_message_queue_size(config.tracking.hz)
        )
        self._receivers: dict[str, _ReceiverHandle] = {}
        self._running = False
        self._shutdown = threading.Event()
        self._shutdown_done = False
        # Insertion-ordered: the first key is the oldest deferred flight.
        self._pending_finalize: dict[str, dict] = {}
        self._pending_evicted = 0
        self._dropped_messages = 0
        self._last_drop_log = 0.0
        self._yielded_writer = False
        # stage -> (monotonic time of the last logged traceback, suppressed count)
        self._stage_errors: dict[str, tuple[float, int]] = {}
        self._history_retry_at = 0.0
        self._previous_handlers: dict[int, object] = {}
        # Frames younger than _MERGE_WINDOW, held for the next tick's merge.
        self._held_frames: list[RawFrame] = []
        self._processed_since_status = 0
        # Cumulative since start, published in the heartbeat (`counters`).
        self._started_at = time.time()
        self._frames_received = 0
        self._frames_dropped = 0
        self._drop_lock = threading.Lock()  # drops are noted on receiver threads
        self._messages_processed = 0
        self._pending_evicted_total = 0
        self._stage_error_counts: dict[str, int] = {}
        self._receiver_restarts: dict[str, int] = {}
        self._max_flight_seconds = float(
            getattr(config.tracking, "max_flight_seconds", _DEFAULT_MAX_FLIGHT_SECONDS)
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
            self._archive_startup_live_flights()
            self._close_stray_active_alerts()

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
        def emit(
            msg_hex: str,
            timestamp: float,
            *,
            rssi: float | None = None,
            clock: int | None = None,
            bearing: float | None = None,
            elevation: float | None = None,
            cfo: float | None = None,
        ) -> None:
            self._enqueue_message(
                msg_hex,
                timestamp,
                name,
                rssi=rssi,
                clock=clock,
                bearing=bearing,
                elevation=elevation,
                cfo=cfo,
            )

        try:
            receiver = create_receiver(method, name, emit, arguments)
        except (KeyError, ValueError, TypeError, OSError) as exc:
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
        self,
        msg_hex: str,
        timestamp: float,
        receiver_name: str,
        *,
        rssi: float | None = None,
        clock: int | None = None,
        bearing: float | None = None,
        elevation: float | None = None,
        cfo: float | None = None,
    ) -> None:
        """Push a raw frame onto the bounded queue, dropping the oldest if full."""
        item = RawFrame(
            hex=msg_hex,
            timestamp=timestamp,
            receiver=receiver_name,
            rssi=rssi,
            clock=clock,
            bearing=bearing,
            elevation=elevation,
            cfo=cfo,
        )
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
        with self._drop_lock:
            self._frames_dropped += count
            self._dropped_messages += count
            now = time.monotonic()
            if now - self._last_drop_log < _DROP_LOG_INTERVAL:
                return
            since_report = self._dropped_messages
            self._dropped_messages = 0
            self._last_drop_log = now
        log.warning(
            "Message queue full (max %d); dropped %d frame(s) since last report",
            self._message_queue.maxsize,
            since_report,
        )

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
            self._receiver_restarts[name] = self._receiver_restarts.get(name, 0) + 1
            handle.receiver.stop()
            cfg = self.config.receivers[name]
            next_backoff = handle.backoff
            self._receivers.pop(name, None)
            self._start_receiver(
                name, cfg.type, cfg.receiver_arguments(), backoff=next_backoff
            )

    def _drain_messages(self) -> list[RawFrame]:
        batch: list[RawFrame] = []
        while True:
            try:
                batch.append(self._message_queue.get_nowait())
            except queue.Empty:
                break
        # Counted here on the main thread rather than per frame on the
        # receiver threads: no lock on the hot path.
        self._frames_received += len(batch)
        return batch

    def _outgoing_frames(
        self, frames: list[RawFrame]
    ) -> tuple[list[RawFrame], list[tuple[str, float]], dict[str, tuple[str, ...]]]:
        """Merge dual-receiver copies, then drop recently seen exact frames.

        Also returns, per outgoing frame hex, every receiver that heard it.
        ``outgoing`` (published on ``live:raw`` and re-served as Beast, e.g.
        to OpenSky) also leaves out extended squitters that fail CRC. The
        tracker rejects those itself, and the raw archive stores every copy
        before this point.
        """
        merged = merge_receiver_frames(frames, window=_MERGE_WINDOW)
        pairs = [(frame.hex, frame.timestamp) for frame, _heard in merged]
        new_messages = self.tracker.collect_new_messages(pairs)
        outgoing_hex = {hex_msg for hex_msg, _ts in new_messages}
        outgoing = [
            frame
            for frame, _heard in merged
            if frame.hex in outgoing_hex and es_crc_ok(frame.hex)
        ]
        heard_by = {
            frame.hex: heard
            for frame, heard in merged
            if heard and frame.hex in outgoing_hex
        }
        return outgoing, new_messages, heard_by

    def _archive_frames(self, frames: list[RawFrame]) -> None:
        """Store every receiver copy (before merge) when raw archiving is on."""
        archive = self.config.archive
        if not archive.raw_frames:
            return
        if frames:
            self.history_store.write_frames(frames)
        now = time.monotonic()
        keep = archive.raw_frames_keep_hours
        if keep > 0 and now - self._last_frame_prune >= _FRAME_PRUNE_INTERVAL:
            pruned = self.history_store.prune_frames(time.time() - keep * 3600)
            # A full batch means expired frames remain: keep pruning each tick
            # until caught up. One batch per interval falls behind a busy site
            # and the table grows without bound.
            if pruned < FRAME_PRUNE_BATCH:
                self._last_frame_prune = now

    def _publish_raw(
        self,
        frames: list[RawFrame],
        heard_by: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        """Fan raw sensor frames out to the live store (Redis pub/sub)."""
        if not frames:
            return
        payloads = [
            raw_payload(frame, receivers=(heard_by or {}).get(frame.hex))
            for frame in frames
        ]
        for offset in range(0, len(payloads), _RAW_PUBLISH_BATCH):
            self.live_store.publish_raw(payloads[offset : offset + _RAW_PUBLISH_BATCH])

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
                len(self._receivers),
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
                    self._yielded_writer = True
                    break
                processed = self._tick()
                with self._guard("status"):
                    self._log_status(processed)

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

    def _tick(self) -> int:
        """Run one pass of the pipeline. Returns the number of messages ingested.

        Each stage is guarded on its own: a bug or outage in one (integrity,
        alerting, Redis, SQLite) is logged and the others keep running, rather
        than the exception ending ``run()`` and all tracking with it.
        """
        guard = self._guard
        with guard("receiver restart"):
            self._restart_dead_receivers()

        raw = self._release_frames(self._drain_messages(), time.time())
        processed = self._process_frames(raw)

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
            self._expire_planes(time.time())
        with guard("segments"):
            self._split_long_flights()
        self._catch_up_archive(time.monotonic() + _ARCHIVE_TICK_BUDGET)
        with guard("pending pops"):
            self.live_store.retry_pending_pops()
        return processed

    def _process_frames(self, raw: list[RawFrame]) -> int:
        """Integrity, raw archive, merge, raw feed, and tracking for one batch."""
        guard = self._guard
        # Integrity sees every copy before merge/de-dup: per-receiver
        # copies drive TDOA, exact repeats reveal replay.
        with guard("integrity observe"):
            self.integrity.observe_frames(raw)
        with guard("raw frame archive"):
            self._archive_frames(raw)
        outgoing: list[RawFrame] = []
        new_messages: list[tuple[str, float]] = []
        heard_by: dict[str, tuple[str, ...]] = {}
        with guard("merge"):
            outgoing, new_messages, heard_by = self._outgoing_frames(raw)
        with guard("raw publish"):
            self._publish_raw(outgoing, heard_by)
        processed = 0
        with guard("tracking"):
            processed = self.tracker.ingest(new_messages, receivers=heard_by)
        self._messages_processed += processed
        with guard("track start"):
            self._mark_track_started()
        return processed

    def _release_frames(
        self, frames: list[RawFrame], now: float, *, flush: bool = False
    ) -> list[RawFrame]:
        """Frames ready for this tick; the rest wait for the next one.

        Copies are merged (and de-duplicated) per batch, so a copy arriving
        after the drain used to be dropped, losing that receiver from the
        sample's provenance and from TDOA. A frame younger than the merge
        window is held, and so is an older copy of the same transmission
        held with it, so every copy is merged together. Nothing waits once it
        is older than the window; a timestamp further in the future (a skewed
        source clock) is not held either, since waiting would not help.
        """
        batch = self._held_frames + frames if self._held_frames else frames
        self._held_frames = []
        if flush or not batch:
            return batch
        low, high = now - _MERGE_WINDOW, now + _MERGE_WINDOW
        young: dict[tuple[int, str], float] = {}
        for frame in batch:
            if low < frame.timestamp < high:
                key = _merge_key(frame.hex)
                young[key] = min(young.get(key, frame.timestamp), frame.timestamp)
        if not young:
            return batch
        release: list[RawFrame] = []
        for frame in batch:
            stamp = frame.timestamp
            if low < stamp < high:
                self._held_frames.append(frame)
                continue
            first = young.get(_merge_key(frame.hex))
            if first is not None and first - _MERGE_WINDOW <= stamp < first:
                self._held_frames.append(frame)
            else:
                release.append(frame)
        return release

    def _publish_health(self) -> None:
        """History health for the ``live:engine`` heartbeat (compose healthcheck)."""
        health = self.history_store.health()
        self.live_store.set_engine_health(
            history=health["state"],
            history_error=health["error"],
            history_pending=len(self._pending_finalize),
            counters=self._counters(),
        )

    def _catch_up_archive(self, deadline: float | None) -> None:
        with self._guard("pending archive"):
            self._retry_pending_finalizes(deadline)
        with self._guard("orphan archive"):
            self._retry_orphan_live_flights(deadline)

    def _split_long_flights(self) -> None:
        """Archive tracks longer than tracking.max_flight_seconds as segments."""
        limit = self._max_flight_seconds
        if limit <= 0:
            return
        for plane in list(self.tracker.planes.values()):
            internal = plane.get(STORE_INTERNAL) or {}
            first = internal.get(STORE_FIRST_PACKET)
            last = internal.get(STORE_MOST_RECENT_PACKET)
            if not isinstance(first, (int, float)) or not isinstance(
                last, (int, float)
            ):
                continue
            if last - first > limit:
                with self._guard("segment"):
                    self._split_flight(plane)

    def _split_flight(self, plane: dict) -> None:
        """Archive the track so far and keep tracking the aircraft as a new flight.

        The new flight starts at the last position sample, which both
        segments share, and keeps the last value of every series so speed,
        heading, ETA, and the verdict in effect carry on without a gap.
        Active alert episodes move to the new flight silently.
        """
        internal = plane[STORE_INTERNAL]
        old_first = internal[STORE_FIRST_PACKET]
        last_packet = internal[STORE_MOST_RECENT_PACKET]
        recv = plane.get(STORE_RECV_DATA) or {}
        lat, lon = recv.get(STORE_LAT) or [], recv.get(STORE_LONG) or []
        last_position = min(lat[-1].time, lon[-1].time) if lat and lon else None
        # The shared boundary sample only when it is recent: after a long
        # stretch without positions (Mode S only) it would make the segments
        # overlap by hours.
        position_is_fresh = (
            last_position is not None
            and last_packet - last_position <= self.config.tracking.remember_planes
            and int(last_position) > int(old_first)
        )
        new_first = last_position if position_is_fresh else last_packet
        if int(new_first) <= int(old_first):
            return  # no distinct flight id possible yet
        internal.setdefault(STORE_TRACK_STARTED, old_first)
        old_flight_id = flight_id_for_plane(plane)
        # Snapshot first: from here on the segment is archived (or kept for
        # retry) whatever the later steps do, so a bug cannot lose the track.
        segment = _segment_copy(plane)
        new_flight_id = None
        try:
            self._start_new_segment(plane, new_first, keep_position=position_is_fresh)
            new_flight_id = flight_id_for_plane(plane)
        except Exception:
            self._stage_failed("segment trim")
        if new_flight_id is not None:
            carried = 0
            with self._guard("segment alerts"):
                carried = self.live_store.carry_active_alerts(
                    old_flight_id, new_flight_id
                )
            with self._guard("segment alerts"):
                # Switch the alert engine to the new flight now, so an orphan
                # sweep of the old segment can never send a false deactivate.
                self.calculator.handover(plane, old_flight_id, new_flight_id)
            with self._guard("live store"):
                # Live before the old copy is popped: no tick without it.
                self.live_store.write_live_planes({new_flight_id: plane})
            log.info(
                "Flight %s exceeded tracking.max_flight_seconds (%.0f s); archived "
                "it and continue as %s%s",
                old_flight_id,
                self._max_flight_seconds,
                new_flight_id,
                f" ({carried} active alert(s) carried over)" if carried else "",
            )
        self._archive_and_pop(segment, from_snapshot=False)

    @staticmethod
    def _start_new_segment(
        plane: dict, new_first: float, *, keep_position: bool
    ) -> None:
        """Trim the live plane to the start of its next segment.

        The last value of every series is kept (speed, heading, ETA, and the
        verdict in effect carry on); a stale last position is dropped so the
        next segment has no sample before its start.
        """
        internal = plane[STORE_INTERNAL]
        for bucket in (STORE_RECV_DATA, STORE_CALC_DATA):
            for series in (plane.get(bucket) or {}).values():
                del series[:-1]
        if not keep_position:
            recv = plane.get(STORE_RECV_DATA) or {}
            for key in (STORE_LAT, STORE_LONG):
                if key in recv:
                    recv[key].clear()
        heard = internal.get(STORE_POSITION_RECEIVERS)
        if heard:
            for stamp in [s for s in heard if s < new_first]:
                del heard[stamp]
        verdicts = internal.get(STORE_INTEGRITY_LOG)
        if verdicts:
            in_effect = [v for v in verdicts if v.time <= new_first][-1:]
            verdicts[:] = in_effect + [v for v in verdicts if v.time > new_first]
        internal[STORE_FIRST_PACKET] = new_first

    def _mark_track_started(self) -> None:
        """Record when the engine first tracked each aircraft.

        Kept across segment splits, which reset STORE_FIRST_PACKET, so checks
        that need a track's real age (integrity) can read it.
        """
        for plane in self.tracker.planes.values():
            internal = plane.get(STORE_INTERNAL)
            if internal is not None and STORE_TRACK_STARTED not in internal:
                first = internal.get(STORE_FIRST_PACKET)
                if first is not None:
                    internal[STORE_TRACK_STARTED] = first

    def _expire_planes(self, now: float) -> None:
        expired = self.tracker.expired_planes(now)
        if not expired:
            return
        removed = self.tracker.remove_planes(expired)
        for plane in removed:
            with self._guard("finalize"):
                self._finalize_plane(plane)
        with self._guard("integrity forget"):
            for icao in expired:
                self.integrity.forget(icao)
        log.debug("Expired %d plane(s): %s", len(expired), expired)

    def _log_status(self, processed: int) -> None:
        """INFO status every status_interval, counting messages since the last."""
        self._processed_since_status += processed
        now_mono = time.monotonic()
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

    @contextlib.contextmanager
    def _guard(self, stage: str) -> Iterator[None]:
        """Contain an unexpected exception from one stage so the rest still runs."""
        try:
            yield
        except Exception:
            self._stage_failed(stage)

    def _counters(self) -> dict[str, object]:
        """Heartbeat ``counters``: cumulative since ``since`` (process start).

        ``planes_tracked`` is the current count. ``frames_received`` counts
        frames taken off the intake queue; ``frames_dropped`` those discarded
        because it was full (together: everything the receivers delivered).
        """
        return {
            "since": self._started_at,
            "frames_received": self._frames_received,
            "frames_dropped": self._frames_dropped,
            "messages_processed": self._messages_processed,
            "planes_tracked": len(self.tracker.planes),
            "pending_evicted": self._pending_evicted_total,
            "receiver_restarts": dict(self._receiver_restarts),
            "stage_errors": dict(self._stage_error_counts),
        }

    def _stage_failed(self, stage: str) -> None:
        """Log the exception being handled; one traceback per stage per interval."""
        self._stage_error_counts[stage] = self._stage_error_counts.get(stage, 0) + 1
        now = time.monotonic()
        last, suppressed = self._stage_errors.get(stage, (float("-inf"), 0))
        if now - last < _STAGE_ERROR_LOG_INTERVAL:
            self._stage_errors[stage] = (last, suppressed + 1)
            return
        more = f" ({suppressed} more since the last report)" if suppressed else ""
        log.exception("Engine stage %r failed%s; continuing", stage, more)
        self._stage_errors[stage] = (now, 0)

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
        for handle in self._receivers.values():
            with self._guard(f"stop receiver {handle.name}"):
                handle.receiver.stop()
        for handle in self._receivers.values():
            if handle.thread.ident is not None:
                handle.thread.join(timeout=2.0)

        if not self._yielded_writer:
            # Held and still-queued frames belong to the flights archived now.
            with self._guard("flush frames"):
                self._process_frames(
                    self._release_frames(self._drain_messages(), 0.0, flush=True)
                )

        if self._yielded_writer:
            log.info(
                "Yielded live writer; leaving Redis and in-flight archive "
                "to the other engine"
            )
            self.tracker.planes.clear()
        else:
            for plane in list(self.tracker.planes.values()):
                with self._guard("finalize"):
                    self._finalize_plane(plane)
            self.tracker.planes.clear()
            # Last chance regardless of back-off: a flight still pending now is
            # only recoverable from its clipped live copy at the next start.
            self._history_retry_at = 0.0
            self._catch_up_archive(None)
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

    def _finalize_plane(self, plane: dict, *, from_snapshot: bool = False) -> bool:
        """Deactivate alerts, persist to SQLite, then drop the live Redis copy.

        Redis is only popped after the archive accepts the write (or the flight
        is intentionally not retained) so a disk outage cannot lose history.
        Returns False when the flight was deferred for retry.
        """
        # A broken alerter must not keep the flight out of the archive.
        with self._guard("alert deactivation"):
            self.calculator.deactivate_plane(plane)
        return self._archive_and_pop(plane, from_snapshot=from_snapshot)

    def _archive_and_pop(self, plane: dict, *, from_snapshot: bool) -> bool:
        """Persist a flight whose alerts are closed, then pop its live copy."""
        flight_id = flight_id_for_plane(plane)
        if self._persist(flight_id, plane, from_snapshot=from_snapshot):
            self._pending_finalize.pop(flight_id, None)
            self.live_store.pop_flight(flight_id)
            return True
        log.warning(
            "Deferred persist for %s; keeping live data until history is writable",
            flight_id,
        )
        self._enqueue_pending_finalize(flight_id, plane)
        return False

    def _persist(self, flight_id: str, plane: dict, *, from_snapshot: bool) -> bool:
        """Archive one flight with its closed alerts. False keeps it for retry.

        An unexpected exception (a bug, not an outage) also returns False: the
        full in-memory plane and its live copy are kept instead of being lost
        with the engine.
        """
        try:
            alerts = [
                _closed_alert(alert)
                for alert in self.live_store.get_alerts(
                    flight_id=flight_id, active_only=False
                )
            ]
            store = self.history_store
            # A disabled archive writes nothing: skip the policy work.
            retained = getattr(store, "disabled", False) or self._retain(
                plane, alerts, from_snapshot=from_snapshot
            )
            return store.finalize_plane(plane, alerts=alerts, retained=retained)
        except LiveUnavailable as exc:
            # The flight's alert episodes cannot be read: archive it later
            # rather than without them.
            log.warning("Deferring archive of %s: %s", flight_id, exc)
            return False
        except Exception:
            self._stage_failed("archive")
            return False

    def _retain(self, plane: dict, alerts: list[dict], *, from_snapshot: bool) -> bool:
        """Whether the archive keeps this flight (``archive.flights`` policy).

        ``from_snapshot`` marks a crash/orphan reconstruction from the clipped
        live window: kept whenever it has a position or alerts, and a later
        longer series may upgrade the archive.
        """
        retained = should_retain(
            plane,
            alerts,
            self.config,
            self.polygons,
            positions=_position_samples(plane),
        )
        if not retained and from_snapshot:
            recv = plane.get(STORE_RECV_DATA) or {}
            retained = bool(alerts) or bool(recv.get(STORE_LAT))
        return retained

    def _enqueue_pending_finalize(self, flight_id: str, plane: dict) -> None:
        """Keep a flight for archive retry within _PENDING_FINALIZE_BUDGET_BYTES.

        Over the budget the oldest in-memory planes are dropped. Their live
        copy is not popped, so the orphan sweep still archives them (from the
        live snapshot, clipped to telemetry_keep_seconds) once history is
        writable again.
        """
        self._pending_finalize.pop(flight_id, None)
        self._pending_finalize[flight_id] = plane
        # Recomputed rather than tracked: enqueues only happen while history
        # is failing, and each cost is O(number of series).
        total = sum(_pending_cost(p) for p in self._pending_finalize.values())
        while (
            total > _PENDING_FINALIZE_BUDGET_BYTES and len(self._pending_finalize) > 1
        ):
            oldest = next(iter(self._pending_finalize))
            total -= _pending_cost(self._pending_finalize.pop(oldest))
            if self._pending_evicted == 0:
                log.error(
                    "Deferred archive memory budget (%d MiB) reached; dropping "
                    "the oldest in-memory flights (live copies, clipped to "
                    "tracking.telemetry_keep_seconds, are kept for later archive)",
                    _PENDING_FINALIZE_BUDGET_BYTES >> 20,
                )
            self._pending_evicted += 1
            self._pending_evicted_total += 1

    def _history_writable(self) -> bool:
        store = self.history_store
        return store.disabled or store.ping()

    def _history_retry_due(self) -> bool:
        return time.monotonic() >= self._history_retry_at

    def _back_off_history_retries(self) -> None:
        self._history_retry_at = time.monotonic() + _HISTORY_RETRY_INTERVAL

    def _retry_pending_finalizes(self, deadline: float | None = None) -> None:
        """Archive deferred flights; with *deadline*, stop when it passes."""
        if not self._pending_finalize or not self._history_retry_due():
            return
        if not self._history_writable():
            return
        tracked = {flight_id_for_plane(plane) for plane in self.tracker.planes.values()}
        for index, (flight_id, plane) in enumerate(
            list(self._pending_finalize.items())
        ):
            # At least one per tick, so a slow archive still makes progress.
            if index and deadline is not None and time.monotonic() >= deadline:
                return
            snapshot = flight_id not in tracked
            if not self._persist(flight_id, plane, from_snapshot=snapshot):
                # Disk full or I/O errors fail every write alike (while ping
                # still works): back off instead of retrying, and logging, every
                # pending flight each tick. The failed flight goes to the back
                # so one that keeps failing cannot block the rest.
                del self._pending_finalize[flight_id]
                self._pending_finalize[flight_id] = plane
                self._back_off_history_retries()
                return
            del self._pending_finalize[flight_id]
            self.live_store.pop_flight(flight_id)
        if self._pending_evicted:
            log.warning(
                "History writable again; %d deferred flight(s) were dropped from "
                "memory while it was down and will be archived from live copies",
                self._pending_evicted,
            )
            self._pending_evicted = 0

    def _archive_startup_live_flights(self) -> None:
        """Persist leftover Redis live copies from a previous engine process.

        Whatever fails here stays in Redis for the main loop's orphan sweep.
        """
        with self._guard("startup archive"):
            beat = time.monotonic()
            for plane in self.live_store.planes_for_finalize():
                with self._guard("finalize"):
                    self._finalize_plane(plane, from_snapshot=True)
                beat = self._keep_heartbeat(beat)

    def _keep_heartbeat(self, last: float) -> float:
        """Refresh the heartbeat during long startup work (10 s TTL)."""
        now = time.monotonic()
        if now - last < _STARTUP_HEARTBEAT_INTERVAL:
            return last
        with self._guard("heartbeat"):
            self.live_store.touch_engine()
        return now

    def _close_stray_active_alerts(self) -> None:
        """Close active alerts whose flight never reached ``live:flights``.

        A crash between an alert write and that tick's flight write leaves an
        active alert neither the startup archive nor the orphan sweep can see.
        Each is deactivated like any restart leftover (the notification goes
        out with ``ended_by: restart``), and the closed episode is archived
        with a flight row that has no telemetry: the position is used for the
        notification only, never written as a sample.
        """
        with self._guard("stray alerts"):
            by_flight: dict[str, list[dict]] = {}
            for doc in self.live_store.stray_active_alerts():
                by_flight.setdefault(doc["flight_id"], []).append(doc)
            beat = time.monotonic()
            for flight_id, docs in by_flight.items():
                beat = self._keep_heartbeat(beat)
                plane = _plane_for_stray_alerts(flight_id, docs)
                if plane is None:
                    log.warning("Cannot close stray alerts of %s", flight_id)
                    continue
                log.warning(
                    "Closing %d alert(s) of %s left active by a crash before "
                    "the flight was written",
                    len(docs),
                    flight_id,
                )
                with self._guard("stray alert"):
                    self.calculator.alerts.deactivate(plane, ended_by=ENDED_RESTART)
                    self._archive_and_pop(
                        {**plane, STORE_RECV_DATA: {}}, from_snapshot=True
                    )

    def _retry_orphan_live_flights(self, deadline: float | None = None) -> None:
        """Archive leftover live flights; with *deadline*, stop when it passes."""
        if not self._history_retry_due():
            return
        if deadline is not None and time.monotonic() >= deadline:
            return
        tracked = {flight_id_for_plane(plane) for plane in self.tracker.planes.values()}
        tracked |= set(self._pending_finalize)
        leftover = self.live_store.flight_ids() - tracked
        if leftover and not self._history_writable():
            return
        for flight_id in leftover:
            if deadline is not None and time.monotonic() >= deadline:
                return
            try:
                plane = self.live_store.plane_for_finalize(flight_id)
            except Exception:
                self._stage_failed("orphan snapshot")
                continue
            if plane is not None and not self._finalize_plane(
                plane, from_snapshot=True
            ):
                self._back_off_history_retries()
                return

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
            self._running = False
            self._shutdown.set()

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
