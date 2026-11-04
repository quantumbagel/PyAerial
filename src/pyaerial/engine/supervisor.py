"""Receiver threads and the bounded intake queue they hand frames over through.

Threads: each receiver runs on its own thread and calls :meth:`enqueue`
(through its ``emit``); everything else here runs on the tick thread. The
queue is the only hand-off, and the drop counter (under a lock) the only
other state both sides touch.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pyaerial.config.schema import Config
from pyaerial.engine.guard import StageGuard
from pyaerial.receivers import RawFrame, Receiver, create_receiver
from pyaerial.receivers.frames import es_crc_ok, merge_receiver_frames, raw_payload
from pyaerial.store.archive.writer import FRAME_PRUNE_BATCH

log = logging.getLogger("pyaerial.engine")

RECEIVER_BACKOFF_INITIAL = 1.0
RECEIVER_BACKOFF_MAX = 30.0
_RECEIVER_BACKOFF_RESET_AFTER = 10.0
# Receiver threads -> main loop queue, drained once per tick. It must hold a
# tick's frames plus the backlog of a slow tick or two, or drop-oldest throws
# away data the engine would have caught up on: a fixed 10k capped intake at
# 20k frames/s at hz=2, and a 900-aircraft, 4-receiver profile (~20.5k
# frames/s) dropped 2.7%. Sized for 3 ticks at a generous 30k frames/s
# peak (45k at hz=2). Floor: 1 s of peak for fast ticks, since one GC pause or
# slow archive write spans several of them. Ceiling: queued frames measure
# ~330 B each (with the arrival time), so 240k (8 s of peak) is ~80 MB;
# beyond that the engine is not keeping up and dropping is the right answer.
PEAK_FRAMES_PER_SECOND = 30_000
_MESSAGE_QUEUE_TICKS = 3
MESSAGE_QUEUE_MIN = PEAK_FRAMES_PER_SECOND
MESSAGE_QUEUE_MAX = 8 * PEAK_FRAMES_PER_SECOND
_DROP_LOG_INTERVAL = 10.0
_RAW_PUBLISH_BATCH = 2_000
_FRAME_PRUNE_INTERVAL = 600.0
# Receiver copies of one transmission carry receive stamps within this many
# seconds of each other; merge_receiver_frames uses the same window. Every
# frame also waits this long after it reaches the intake queue, so a late copy
# (another receiver, the next TCP chunk) is merged with the rest instead of
# being dropped as a duplicate.
MERGE_WINDOW = 0.1
# No frame waits longer than this after reaching the queue, whatever copies
# keep arriving (bounds latency and the held list against a stuck clock).
MAX_HOLD = 5 * MERGE_WINDOW
# merge_receiver_frames merges copies differing in at most this many bits.
NEAR_COPY_BITS = 2


def message_queue_size(hz: float) -> int:
    """Frames the intake queue holds: ~3 ticks at peak rate, within bounds."""
    wanted = math.ceil(PEAK_FRAMES_PER_SECOND * _MESSAGE_QUEUE_TICKS / hz)
    return max(MESSAGE_QUEUE_MIN, min(MESSAGE_QUEUE_MAX, wanted))


def _merge_key(hex_msg: str) -> tuple[int, str]:
    """Coarse transmission identity: length plus DF/CA and address bytes."""
    return len(hex_msg), hex_msg[:8]


def _copy_of_any(frame: RawFrame, others: list[RawFrame] | None) -> bool:
    """Whether *frame* is another receiver's copy of a transmission in
    *others* (same coarse key), as :func:`merge_receiver_frames` would merge
    it: stamps within the window, at most ``NEAR_COPY_BITS`` differing bits."""
    bits: int | None = None
    for other in others or ():
        if (
            other.receiver == frame.receiver
            or abs(other.timestamp - frame.timestamp) > MERGE_WINDOW
        ):
            continue
        if other.hex == frame.hex:
            return True
        try:
            if bits is None:
                bits = int(frame.hex, 16)
            differing = (bits ^ int(other.hex, 16)).bit_count()
        except ValueError:
            continue
        if differing <= NEAR_COPY_BITS:
            return True
    return False


class FrameArchive(Protocol):
    """The raw frame side of :class:`~pyaerial.store.ArchiveWriter`."""

    def write_frames(self, frames: list[RawFrame]) -> bool: ...

    def prune_frames(self, before: float) -> int: ...


class RawPublisher(Protocol):
    """The ``live:raw`` side of the live store."""

    def publish_raw(self, messages: list[dict[str, Any]]) -> None: ...


class FrameDeduper(Protocol):
    """Drops recently seen exact frames (the tracker; tick thread only)."""

    def collect_new_messages(
        self, incoming: list[tuple[str, float]]
    ) -> list[tuple[str, float]]: ...


@dataclass
class ReceiverHandle:
    """One supervised receiver: its thread and restart back-off."""

    name: str
    method: str
    receiver: Receiver
    thread: threading.Thread
    last_error: str = ""
    backoff: float = RECEIVER_BACKOFF_INITIAL
    next_restart_at: float = 0.0
    started_at: float = 0.0


class ReceiverSupervisor:
    """Starts, restarts and stops receivers; owns the intake queue.

    Per tick the engine calls :meth:`restart_dead`, then :meth:`drain`, and
    hands the batch to :meth:`archive_frames`, :meth:`merge` and
    :meth:`publish_raw` (each under its own stage guard).

    Test hooks: :meth:`enqueue` is the receivers' entry point, ``receivers``
    may be given a hand-made :class:`ReceiverHandle`, ``queue`` may be
    replaced by a smaller one, :attr:`held` shows the frames waiting for
    the next tick's merge, and ``clock`` (monotonic seconds; stamps arrival
    in the queue and drives the hold-back) may be replaced.
    """

    def __init__(
        self,
        config: Config,
        guard: StageGuard,
        shutdown: threading.Event,
        *,
        archive: FrameArchive,
        publisher: RawPublisher,
        dedupe: FrameDeduper,
    ):
        self.config = config
        self._guard = guard
        # Set by the engine (signal or shutdown): no more restarts.
        self._shutdown = shutdown
        self._archive = archive
        self._publisher = publisher
        self._dedupe = dedupe
        self.clock: Callable[[], float] = time.monotonic
        # (arrival per ``clock``, frame). The arrival is not the receive stamp:
        # a frame can be stamped well before it is queued (dump1090 stamps are
        # spread back from the chunk's arrival by 12 MHz clock deltas, and a
        # receiver thread can be descheduled in between).
        self.queue: queue.Queue[tuple[float, RawFrame]] = queue.Queue(
            maxsize=message_queue_size(config.tracking.hz)
        )
        self.receivers: dict[str, ReceiverHandle] = {}
        # Frames that may still merge with a later copy, for the next tick.
        self._held_frames: list[tuple[float, RawFrame]] = []
        # -inf, not 0: monotonic() can be under the interval on a fresh boot.
        self._last_frame_prune = float("-inf")
        self._dropped_messages = 0
        self._last_drop_log = 0.0
        # Cumulative since start, published in the heartbeat (`counters`).
        self._frames_received = 0
        self._frames_dropped = 0
        self._drop_lock = threading.Lock()  # drops are noted on receiver threads
        self._receiver_restarts: dict[str, int] = {}

    # -- receiver threads --------------------------------------------------

    def start(self) -> None:
        """Start every configured receiver; raise if none could start."""
        for name, receiver_cfg in self.config.receivers.items():
            self.start_receiver(
                name, receiver_cfg.type, receiver_cfg.receiver_arguments()
            )

        if not self.receivers:
            raise RuntimeError("no receivers could be started")

    def start_receiver(
        self,
        name: str,
        method: str,
        arguments: dict,
        *,
        backoff: float = RECEIVER_BACKOFF_INITIAL,
    ) -> None:
        """Create receiver *name* and start its thread (logged on failure)."""

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
            self.enqueue(
                RawFrame(
                    hex=msg_hex,
                    timestamp=timestamp,
                    receiver=name,
                    rssi=rssi,
                    clock=clock,
                    bearing=bearing,
                    elevation=elevation,
                    cfo=cfo,
                )
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
        self.receivers[name] = ReceiverHandle(
            name,
            method,
            receiver,
            thread,
            backoff=backoff,
            started_at=time.monotonic(),
        )
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
        handle = self.receivers.get(name)
        if handle is not None:
            handle.last_error = reason or ""

    def restart_dead(self) -> None:
        """Restart stopped receivers, with exponential back-off per receiver."""
        now = time.monotonic()
        for name, handle in list(self.receivers.items()):
            if handle.thread.is_alive():
                continue
            if self._shutdown.is_set():
                return
            if handle.next_restart_at <= 0.0:
                if now - handle.started_at >= _RECEIVER_BACKOFF_RESET_AFTER:
                    handle.backoff = RECEIVER_BACKOFF_INITIAL
                handle.next_restart_at = now + handle.backoff
                log.warning(
                    "Receiver %r (%s) stopped%s; retry in %.1fs",
                    name,
                    handle.method,
                    f": {handle.last_error}" if handle.last_error else "",
                    handle.backoff,
                )
                handle.backoff = min(handle.backoff * 2.0, RECEIVER_BACKOFF_MAX)
                continue
            if now < handle.next_restart_at:
                continue
            log.warning("Restarting receiver %r (%s)", name, handle.method)
            self._receiver_restarts[name] = self._receiver_restarts.get(name, 0) + 1
            handle.receiver.stop()
            cfg = self.config.receivers[name]
            next_backoff = handle.backoff
            self.receivers.pop(name, None)
            self.start_receiver(
                name, cfg.type, cfg.receiver_arguments(), backoff=next_backoff
            )

    def stop(self) -> None:
        """Ask every receiver to stop, then wait briefly for its thread."""
        for handle in self.receivers.values():
            with self._guard(f"stop receiver {handle.name}"):
                handle.receiver.stop()
        for handle in self.receivers.values():
            if handle.thread.ident is not None:
                handle.thread.join(timeout=2.0)

    # -- intake queue ------------------------------------------------------

    def enqueue(self, frame: RawFrame) -> None:
        """Push a raw frame onto the bounded queue, dropping the oldest if full.

        Called on receiver threads (thread-safe).
        """
        entry = (self.clock(), frame)
        try:
            self.queue.put_nowait(entry)
            return
        except queue.Full:
            pass
        dropped = 0
        try:
            self.queue.get_nowait()
            dropped += 1
        except queue.Empty:
            pass
        try:
            self.queue.put_nowait(entry)
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
            self.queue.maxsize,
            since_report,
        )

    def drain(self, now: float | None = None) -> list[RawFrame]:
        """Frames ready for this tick: the queue plus held frames, minus
        the copies that may still merge with a later one (see :meth:`_release`).

        *now* (``clock`` seconds) defaults to ``clock()`` read before the
        queue is taken, so a frame that lands meanwhile only looks younger.
        """
        if now is None:
            now = self.clock()
        return self._release(self._take_queued(), now)

    def flush(self) -> list[RawFrame]:
        """Everything queued and held, nothing held back (shutdown)."""
        return self._release(self._take_queued(), 0.0, flush=True)

    @property
    def held(self) -> tuple[RawFrame, ...]:
        """Frames waiting for the next tick's merge."""
        return tuple(frame for _arrived, frame in self._held_frames)

    def _take_queued(self) -> list[tuple[float, RawFrame]]:
        batch: list[tuple[float, RawFrame]] = []
        while True:
            try:
                batch.append(self.queue.get_nowait())
            except queue.Empty:
                break
        # Counted here on the main thread rather than per frame on the
        # receiver threads: no lock on the hot path.
        self._frames_received += len(batch)
        return batch

    def _release(
        self,
        entries: list[tuple[float, RawFrame]],
        now: float,
        *,
        flush: bool = False,
    ) -> list[RawFrame]:
        """Frames ready for this tick; the rest wait for the next one.

        Copies are merged (and de-duplicated) per batch, so a copy arriving
        after the drain used to be dropped, losing that receiver from the
        sample's provenance and from TDOA. A frame is held until
        ``MERGE_WINDOW`` after it reached the queue, and so is any earlier
        copy of the same transmission held with it: another receiver's
        frame, stamped within the window, with the same or near-identical
        bytes (as :func:`merge_receiver_frames` matches them; never just
        the same aircraft, whose next squitter must not hold back one copy
        of the previous and release the other).

        Holding by arrival rather than by stamp matters: a copy stamped
        well before it reached the queue looked old already and went out
        alone, just before its partner arrived. Latency: a frame waits at
        least ``MERGE_WINDOW`` (up to one tick more) and never more than
        ``MAX_HOLD`` (plus a tick) after it reached the queue.
        """
        batch = self._held_frames + entries if self._held_frames else entries
        self._held_frames = []
        if flush or not batch:
            return [frame for _arrived, frame in batch]
        young_after = now - MERGE_WINDOW
        young: dict[tuple[int, str], list[RawFrame]] = {}
        for arrived, frame in batch:
            if arrived > young_after:
                young.setdefault(_merge_key(frame.hex), []).append(frame)
        if not young:
            return [frame for _arrived, frame in batch]
        expired_before = now - MAX_HOLD
        release: list[RawFrame] = []
        for entry in batch:
            arrived, frame = entry
            if arrived > young_after or (
                arrived > expired_before
                and _copy_of_any(frame, young.get(_merge_key(frame.hex)))
            ):
                self._held_frames.append(entry)
            else:
                release.append(frame)
        return release

    # -- per-batch frame stages --------------------------------------------

    def archive_frames(self, frames: list[RawFrame]) -> None:
        """Store every receiver copy (before merge) when raw archiving is on."""
        archive = self.config.archive
        if not archive.raw_frames:
            return
        if frames:
            self._archive.write_frames(frames)
        now = time.monotonic()
        keep = archive.raw_frames_keep_hours
        if keep > 0 and now - self._last_frame_prune >= _FRAME_PRUNE_INTERVAL:
            pruned = self._archive.prune_frames(time.time() - keep * 3600)
            # A full batch means expired frames remain: keep pruning each tick
            # until caught up. One batch per interval falls behind a busy site
            # and the table grows without bound.
            if pruned < FRAME_PRUNE_BATCH:
                self._last_frame_prune = now

    def merge(
        self, frames: list[RawFrame]
    ) -> tuple[list[RawFrame], list[tuple[str, float]], dict[str, tuple[str, ...]]]:
        """Merge dual-receiver copies, then drop recently seen exact frames.

        Also returns, per outgoing frame hex, every receiver that heard it.
        ``outgoing`` (published on ``live:raw`` and re-served as Beast, e.g.
        to OpenSky) also leaves out extended squitters that fail CRC. The
        tracker rejects those itself, and the raw archive stores every copy
        before this point.
        """
        merged = merge_receiver_frames(
            frames, window=MERGE_WINDOW, max_hamming=NEAR_COPY_BITS
        )
        pairs = [(frame.hex, frame.timestamp) for frame, _heard in merged]
        new_messages = self._dedupe.collect_new_messages(pairs)
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

    def publish_raw(
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
            self._publisher.publish_raw(payloads[offset : offset + _RAW_PUBLISH_BATCH])

    # -- health --------------------------------------------------------------

    def counters(self) -> dict[str, Any]:
        """Cumulative since start: ``frames_received`` (taken off the queue),
        ``frames_dropped`` (discarded because it was full) and
        ``receiver_restarts`` per receiver."""
        return {
            "frames_received": self._frames_received,
            "frames_dropped": self._frames_dropped,
            "receiver_restarts": dict(self._receiver_restarts),
        }
