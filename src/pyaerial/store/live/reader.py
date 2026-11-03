"""The portal's read-only view of the Redis live store.

:class:`LiveReader` answers from Redis only: it keeps no memory mirror, and
raises :class:`LiveUnavailable` when Redis cannot be read rather than
answer with nothing. It has no write methods; the engine's
:class:`~pyaerial.store.live.writer.LiveWriter` owns the keys.
"""

from __future__ import annotations

import logging
import time
from typing import Any, NoReturn

import redis
from redis.exceptions import RedisError

from pyaerial.store.live._conn import UNAVAILABLE, LiveUnavailable
from pyaerial.store.live._raw import RawFeed
from pyaerial.store.live._reads import (
    alerts_by_id,
    by_time,
    collect_live_telemetry,
    filter_alerts,
    newest_first,
    read_alerts,
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

log = logging.getLogger("pyaerial.store.redis")


class LiveReader(RawFeed):
    """Read-only live store for the web portal (and other non-engine readers)."""

    def __init__(self, redis_uri: str) -> None:
        super().__init__(redis_uri)
        self._connect()

    def _client(self) -> redis.Redis:
        client = self._redis()
        if client is None:
            raise LiveUnavailable(UNAVAILABLE)
        return client

    def _failed(
        self, exc: RedisError, client: redis.Redis, message: str, *args: Any
    ) -> NoReturn:
        """Log a failed read, drop the client, raise :class:`LiveUnavailable`."""
        log.error(message, *args, exc)
        self._mark_disconnected(exc, client)
        raise LiveUnavailable(UNAVAILABLE)

    def get_flights(self) -> list[dict[str, Any]]:
        client = self._client()
        try:
            return newest_first(read_flight_summaries(client))
        except RedisError as exc:
            self._failed(exc, client, "Failed to read live flights from Redis: %s")

    def count_flights(self) -> int:
        """Number of live flights (SCARD; no documents are fetched)."""
        client = self._client()
        try:
            return int(client.scard(KEY_FLIGHTS))
        except RedisError as exc:
            self._failed(exc, client, "Failed to count live flights: %s")

    def get_flight(self, flight_id: str) -> dict[str, Any] | None:
        client = self._client()
        try:
            return read_flight(client, flight_id)
        except RedisError as exc:
            self._failed(exc, client, "Failed to read live flight %s: %s", flight_id)

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
        client = self._client()
        try:
            return read_telemetry(client, flight_id, since, limit=limit, newest=newest)
        except RedisError as exc:
            self._failed(
                exc, client, "Failed to read telemetry for %s: %s", flight_id
            )

    def latest_telemetry(self, flight_id: str) -> dict[str, Any] | None:
        """The flight's newest telemetry point (one ZREVRANGE), or None."""
        client = self._client()
        try:
            return read_latest_telemetry(client, flight_id)
        except RedisError as exc:
            self._failed(
                exc, client, "Failed to read latest telemetry for %s: %s", flight_id
            )

    def get_live_telemetry(self, since: float = 0.0) -> list[dict[str, Any]]:
        client = self._client()
        points: list[dict[str, Any]] = []
        try:
            collect_live_telemetry(client, since, points)
        except RedisError as exc:
            self._failed(exc, client, "Failed to read live telemetry: %s")
        return by_time(points)

    def get_alerts(
        self,
        *,
        since: float = 0.0,
        flight_id: str | None = None,
        rule: str | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Alert episodes in ``live:*`` (one per alert_id), newest first."""
        client = self._client()
        try:
            docs = read_alerts(client, flight_id, active_only)
        except RedisError as exc:
            self._failed(exc, client, "Failed to read live alerts: %s")
        return filter_alerts(
            list(alerts_by_id(docs).values()),
            since=since,
            rule=rule,
            active_only=active_only,
        )

    # -- engine heartbeat -----------------------------------------------------

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
        return None

    def engine_is_live(self) -> bool:
        """True when a fresh ``live:engine`` heartbeat exists."""
        seen = self.engine_seen_at()
        if not isinstance(seen, (int, float)):
            return False
        return time.time() - seen < ENGINE_TTL_SECONDS

    def engine_heartbeat(self) -> dict[str, Any] | None:
        """The ``live:engine`` document (fields in CONFIGURATION.md), one GET.

        None when there is no heartbeat or it is not a JSON object. Raises
        :class:`LiveUnavailable` when Redis cannot be read.
        """
        client = self._client()
        try:
            raw = client.get(KEY_ENGINE)
        except RedisError as exc:
            raise LiveUnavailable(f"could not read the engine heartbeat: {exc}") from exc
        return parse_heartbeat(raw)
