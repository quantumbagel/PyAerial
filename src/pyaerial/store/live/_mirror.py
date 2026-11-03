"""Base of the engine's :class:`~pyaerial.store.live.writer.LiveWriter`:
its state, the in-memory mirror of the live keys, and the reads that fall
back to the mirror.

The mirror (:class:`~pyaerial.store.memory.MemoryLiveBuffer`) is the whole
store in isolated mode (``memory_only``), keeps the engine tracking while
Redis is down, and is replayed into Redis on reconnect.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

import redis
from redis.exceptions import RedisError

from pyaerial.store.live._conn import UNAVAILABLE, LiveUnavailable
from pyaerial.store.live._raw import RawFeed
from pyaerial.store.live._reads import (
    by_time,
    collect_live_telemetry,
    live_point,
    newest_first,
    read_flight,
    read_flight_summaries,
    read_latest_telemetry,
    read_telemetry,
)
from pyaerial.store.live.keys import (
    ENGINE_TTL_SECONDS,
    KEY_ENGINE,
    KEY_FLIGHTS,
    heartbeat_seen_at,
    parse_heartbeat,
)
from pyaerial.store.memory import MemoryLiveBuffer
from pyaerial.store.present import live_flight_detail, live_flight_summary

log = logging.getLogger("pyaerial.store.redis")


class MirroredStore(RawFeed):
    """Writer state and mirror-backed reads; see :class:`LiveWriter`."""

    def __init__(
        self,
        redis_uri: str,
        *,
        memory_only: bool,
        telemetry_keep_seconds: float,
        writer: bool,
    ) -> None:
        super().__init__(redis_uri)
        self.memory_only = memory_only
        self.telemetry_keep_seconds = telemetry_keep_seconds
        # Whether this process owns the Redis live keys (claims the heartbeat
        # on reconnect, writes flights and episodes). Cleared on yielding to
        # another engine; False for a maintenance handle (``pyaerial reset``).
        self.writer = writer
        self._last_telemetry_ts: dict[str, float] = {}
        # flight_id -> JSON last SET in Redis, so unchanged flights are skipped.
        self._written_docs: dict[str, str] = {}
        self._mem = MemoryLiveBuffer()
        # Held while changing ``_mem`` and while copying it for the reconnect
        # backfill (which may run on another thread). Never held across I/O.
        self._mem_lock = threading.RLock()
        # Insertion-ordered set of flight ids whose live doc cannot be used.
        self._unreadable_flights: dict[str, None] = {}
        self._pending_pops: set[str] = set()
        # alert_id -> (flight_id, doc): active alert refreshes queued for the
        # tick's write_live_planes pipeline (guarded by _mem_lock).
        self._dirty_alerts: dict[str, tuple[str, dict[str, Any]]] = {}
        # Extra fields the engine publishes in its heartbeat (history health).
        self._engine_health: dict[str, Any] = {}
        self._engine_token = uuid.uuid4().hex

    # -- connection -----------------------------------------------------------

    def _redis(self) -> redis.Redis | None:
        """A connected client snapshot, or None to use the memory buffer."""
        if self.memory_only:
            return None
        return super()._redis()

    def ping(self) -> bool:
        """Return True if this store can serve reads.

        Memory-only mode is always ready. Redis mode is ready when the
        server is reachable; a reconnect is attempted if the client is down.
        """
        if self.memory_only:
            return True
        return self._ensure_connected()

    def _on_disconnect(self) -> None:
        # Reconnect backfill rewrites flights; forget what was sent.
        self._written_docs.clear()

    def _listens_on_redis(self) -> bool:
        return not self.memory_only

    def _reader_needs_redis(self) -> bool:
        return not self.writer and not self.memory_only

    def _raise_if_reader_disconnected(self) -> None:
        if self._reader_needs_redis():
            raise LiveUnavailable(UNAVAILABLE)

    # -- reads (Redis, else the mirror) ---------------------------------------

    def get_flights(self) -> list[dict[str, Any]]:
        client = self._redis()
        if client is None:
            self._raise_if_reader_disconnected()
            results: list[dict[str, Any]] = []
            for doc in self._mem.flights.values():
                last_tel = (
                    self._mem.telemetry[doc["flight_id"]][-1]
                    if self._mem.telemetry.get(doc["flight_id"])
                    else None
                )
                results.append(live_flight_summary(doc, last_tel))
            return newest_first(results)
        try:
            results = read_flight_summaries(client)
        except RedisError as exc:
            log.error("Failed to read live flights from Redis: %s", exc)
            self._mark_disconnected(exc, client)
            self._raise_if_reader_disconnected()
            results = []
        return newest_first(results)

    def count_flights(self) -> int:
        """Number of live flights (SCARD; no documents are fetched)."""
        client = self._redis()
        if client is None:
            self._raise_if_reader_disconnected()
            return len(self._mem.flights)
        try:
            return int(client.scard(KEY_FLIGHTS))
        except RedisError as exc:
            log.error("Failed to count live flights: %s", exc)
            self._mark_disconnected(exc, client)
            self._raise_if_reader_disconnected()
            return len(self._mem.flights)

    def get_flight(self, flight_id: str) -> dict[str, Any] | None:
        client = self._redis()
        if client is None:
            self._raise_if_reader_disconnected()
            doc = self._mem.flights.get(flight_id)
            return live_flight_detail(doc, flight_id) if doc else None
        try:
            return read_flight(client, flight_id)
        except RedisError as exc:
            log.error("Failed to read live flight %s: %s", flight_id, exc)
            self._mark_disconnected(exc, client)
            self._raise_if_reader_disconnected()
            return None

    def get_telemetry(
        self,
        flight_id: str,
        *,
        since: float = 0.0,
        limit: int | None = None,
        newest: bool = False,
    ) -> list[dict[str, Any]]:
        """Telemetry points after ``since``, in ascending time order.

        ``limit`` (> 0) returns at most that many, selected inside Redis so a
        long ring (``telemetry_keep_seconds: 0``) is never read whole: the
        oldest after ``since`` (a caller pages forward with
        ``since=<last timestamp>``), or with ``newest`` the most recent.
        """
        limit = int(limit) if limit and limit > 0 else None
        client = self._redis()
        if client is None:
            self._raise_if_reader_disconnected()
            points = list(self._mem.telemetry.get(flight_id, ()))
            if since > 0:
                points = [p for p in points if p.get("timestamp", 0) > since]
            if limit is not None:
                points = points[-limit:] if newest else points[:limit]
            return points
        try:
            return read_telemetry(client, flight_id, since, limit=limit, newest=newest)
        except RedisError as exc:
            log.error("Failed to read telemetry for %s: %s", flight_id, exc)
            self._mark_disconnected(exc, client)
            self._raise_if_reader_disconnected()
            return []

    def latest_telemetry(self, flight_id: str) -> dict[str, Any] | None:
        """The flight's newest telemetry point (one ZREVRANGE), or None."""
        client = self._redis()
        if client is None:
            self._raise_if_reader_disconnected()
            points = self._mem.telemetry.get(flight_id)
            return points[-1] if points else None
        try:
            return read_latest_telemetry(client, flight_id)
        except RedisError as exc:
            log.error("Failed to read latest telemetry for %s: %s", flight_id, exc)
            self._mark_disconnected(exc, client)
            self._raise_if_reader_disconnected()
            return None

    def get_live_telemetry(self, since: float = 0.0) -> list[dict[str, Any]]:
        client = self._redis()
        points: list[dict[str, Any]] = []
        if client is None:
            self._raise_if_reader_disconnected()
            for flight_id in self._mem.flights:
                for point in self._mem.telemetry.get(flight_id, []):
                    if since > 0 and point.get("timestamp", 0) <= since:
                        continue
                    points.append(live_point(flight_id, point))
        else:
            try:
                collect_live_telemetry(client, since, points)
            except RedisError as exc:
                log.error("Failed to read live telemetry: %s", exc)
                self._mark_disconnected(exc, client)
                self._raise_if_reader_disconnected()
        return by_time(points)

    # -- engine heartbeat (read side) -----------------------------------------

    def engine_seen_at(self) -> float | None:
        """Unix timestamp of the last engine heartbeat, or None if missing."""
        client = self._redis()
        if client is not None:
            try:
                raw = client.get(KEY_ENGINE)
                if raw:
                    seen = heartbeat_seen_at(raw)
                    if seen is not None:
                        return seen
            except (RedisError, TypeError, ValueError) as exc:
                log.debug("Could not read engine heartbeat: %s", exc)
        return self._mem.engine_seen_at

    def engine_is_live(self) -> bool:
        """True when a fresh ``live:engine`` heartbeat exists."""
        seen = self.engine_seen_at()
        if not isinstance(seen, (int, float)):
            return False
        return time.time() - seen < ENGINE_TTL_SECONDS

    def engine_heartbeat(self) -> dict[str, Any] | None:
        """The ``live:engine`` document; see :meth:`LiveReader.engine_heartbeat`.

        Without Redis (memory-only, or down) only ``seen_at`` is known.
        """
        client = self._redis()
        if client is None:
            seen = self._mem.engine_seen_at
            return {"seen_at": seen} if seen is not None else None
        try:
            raw = client.get(KEY_ENGINE)
        except RedisError as exc:
            raise LiveUnavailable(f"could not read the engine heartbeat: {exc}") from exc
        return parse_heartbeat(raw)
