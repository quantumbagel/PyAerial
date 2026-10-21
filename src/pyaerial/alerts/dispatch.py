"""Bounded, per-key ordered background dispatch for alerter calls."""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import logging
import queue
import threading
import time
import zlib

log = logging.getLogger("pyaerial.alerts")

#: How long shutdown waits for queued notifications. One webhook can take
#: ~16 s (3 attempts x 5 s timeout + backoff), so this covers a final
#: deactivate per alert without letting Ctrl-C hang indefinitely.
SHUTDOWN_GRACE_SECONDS = 30.0

_Job = tuple[Callable[..., None], tuple]


class AlertDispatcher:
    """Run alerter calls on a few worker threads without unbounded backlog.

    Jobs sharing a ``key`` (an alert id) always land on the same worker, so an
    alert's activate / while_active / deactivate notifications are delivered in
    the order they were raised. Each worker has a bounded queue; when it is
    full the new job is dropped and logged (a hung webhook must not grow
    memory without limit). :meth:`close` gives in-flight work a short grace
    period, then discards whatever is still queued.
    """

    def __init__(
        self,
        workers: int = 4,
        max_pending_per_worker: int = 256,
        name: str = "alert-dispatch",
    ):
        self._queues: list[queue.Queue[_Job | None]] = [
            queue.Queue(maxsize=max_pending_per_worker) for _ in range(max(1, workers))
        ]
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._closed = False
        self._dropped = 0
        self._dropped_lock = threading.Lock()
        self._threads = [
            threading.Thread(
                target=self._worker, args=(q,), name=f"{name}-{i}", daemon=True
            )
            for i, q in enumerate(self._queues)
        ]
        for thread in self._threads:
            thread.start()

    @property
    def dropped(self) -> int:
        with self._dropped_lock:
            return self._dropped

    def pending(self) -> int:
        return sum(q.qsize() for q in self._queues)

    def submit(self, key: str, fn: Callable[..., None], *args: object) -> bool:
        """Queue ``fn(*args)`` behind earlier jobs for ``key``. False if dropped."""
        if self._closed:
            self._count_drop("dispatcher closed", key)
            return False
        target = self._queues[zlib.crc32(key.encode()) % len(self._queues)]
        try:
            target.put_nowait((fn, args))
        except queue.Full:
            self._count_drop("queue full", key)
            return False
        return True

    def close(self, timeout: float = SHUTDOWN_GRACE_SECONDS) -> None:
        """Stop accepting work, let queued jobs run for up to ``timeout`` s, drop the rest."""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        for q in self._queues:
            # Wake idle workers; a full queue's worker notices _stop once drained.
            with contextlib.suppress(queue.Full):
                q.put_nowait(None)
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        self._cancel.set()
        discarded = 0
        for q in self._queues:
            while True:
                try:
                    job = q.get_nowait()
                except queue.Empty:
                    break
                if job is not None:
                    discarded += 1
        if discarded:
            log.warning("Discarded %d queued alert notification(s) at shutdown", discarded)
        for thread in self._threads:
            thread.join(0.1)

    def _count_drop(self, reason: str, key: str) -> None:
        with self._dropped_lock:
            self._dropped += 1
            total = self._dropped
        # Log the first drop and then every 100th so a stuck alerter can't
        # flood the log as well.
        if total == 1 or total % 100 == 0:
            log.warning(
                "Dropped alert notification for %s (%s); %d dropped so far",
                key,
                reason,
                total,
            )

    def _worker(self, jobs: queue.Queue[_Job | None]) -> None:
        while True:
            try:
                job = jobs.get(timeout=0.1)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            if job is None or self._cancel.is_set():
                return
            fn, args = job
            try:
                fn(*args)
            except Exception:
                log.exception("Alert dispatch job failed")
