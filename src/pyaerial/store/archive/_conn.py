"""Connection handling shared by the archive reader and writer: one SQLite
connection behind a lock, reconnect backoff, and rate-limited state reports."""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

log = logging.getLogger("pyaerial.store")

_RECONNECT_DELAY = 2.0
# A down archive is logged at ERROR again at most this often (seconds).
_DOWN_LOG_INTERVAL = 300.0


class HistoryUnavailable(RuntimeError):
    """Configured SQLite archive cannot be read."""


class LeaseLost(HistoryUnavailable):
    """Another tracking engine took the archive over: this one must stop
    writing it (and yield)."""


class RowRejected(ValueError):
    """The archive is up but refuses a row because of its content (a missing
    key, a wrong type, a constraint): retrying the same row cannot help."""


def unavailable(action: str, exc: sqlite3.Error | None = None) -> HistoryUnavailable:
    if exc is not None:
        log.warning("History database unavailable for %s: %s", action, exc)
        return HistoryUnavailable(f"history archive is unavailable ({action}): {exc}")
    return HistoryUnavailable(f"history archive is unavailable ({action})")


class ArchiveConnection:
    """One connection to the archive file, reopened with backoff when lost.

    Subclasses implement :meth:`_connect`, which opens the file, sets
    ``self._conn`` through :meth:`_opened`, or reports why it could not.
    """

    #: Opened with ``mode=ro``: no schema work, no write lock, nothing created.
    read_only: ClassVar[bool] = False

    def __init__(self, path: str | Path, *, disabled: bool = False):
        self.path = str(Path(path).expanduser())
        if not Path(self.path).is_absolute():
            self.path = str(Path(self.path).resolve())
        self.disabled = disabled
        #: ok | missing | needs_upgrade | incompatible | down | disabled
        self.state = "disabled" if disabled else "down"
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._last_connect_attempt = 0.0
        self._reported_down = False
        # Last failure (connect, query, or write), cleared by a success.
        self._last_error: str | None = None
        self._last_report: tuple[str, float] = ("", float("-inf"))
        if not disabled:
            self._connect()

    def _connect(self) -> None:
        raise NotImplementedError

    def _report(self, state: str, message: str, *, level: int = logging.ERROR) -> None:
        """Log a problem: on a new state, then at most every _DOWN_LOG_INTERVAL."""
        self.state = state
        self._last_error = message
        self._reported_down = True
        last_state, last_at = self._last_report
        now = time.monotonic()
        if state == last_state and now - last_at < _DOWN_LOG_INTERVAL:
            return
        self._last_report = (state, now)
        log.log(level, "%s", message)

    def _opened(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        if self._reported_down:
            log.info("Reconnected to history database %s", self.path)
        else:
            log.info(
                "Opened history database %s%s",
                self.path,
                " (read-only)" if self.read_only else "",
            )
        self._reported_down = False
        self._last_report = ("", float("-inf"))
        self._last_error = None
        self.state = "ok"

    def _reconnect_if_due(self) -> None:
        """Caller holds ``self._lock``."""
        if self._conn is None:
            now = time.monotonic()
            if now - self._last_connect_attempt >= _RECONNECT_DELAY:
                self._last_connect_attempt = now
                self._connect()

    @contextlib.contextmanager
    def _session(self, action: str) -> Iterator[sqlite3.Connection]:
        """Yield the connection with the lock held for the whole block.

        Checking for a connection and then using it under a separately
        acquired lock let another thread close it in between. Raises
        :class:`HistoryUnavailable` when there is no connection.
        """
        if self.disabled:
            raise unavailable(action)
        with self._lock:
            self._reconnect_if_due()
            conn = self._conn
            if conn is None:
                raise unavailable(action)
            yield conn

    def ping(self) -> bool:
        if self.disabled:
            return False
        with self._lock:
            self._reconnect_if_due()
            conn = self._conn
            if conn is None:
                return False
            try:
                conn.execute("SELECT 1").fetchone()
                return True
            except sqlite3.Error as exc:
                self._report(
                    "down", f"Lost history database connection {self.path} ({exc})."
                )
                self._close_conn()
                return False

    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error as exc:
                log.debug("Error closing history database: %s", exc)
            self._conn = None

    def close(self) -> None:
        with self._lock:
            self._close_conn()
