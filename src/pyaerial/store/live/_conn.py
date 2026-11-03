"""Connection handling shared by the live reader and writer: one redis-py
client, a throttled reconnect run by one thread at a time, and the down/up
reports."""

from __future__ import annotations

import logging
import threading
import time

import redis
from redis.exceptions import RedisError

from pyaerial.urls import redact_uri

log = logging.getLogger("pyaerial.store.redis")

_RECONNECT_DELAY = 2.0
UNAVAILABLE = "live store is unavailable"


class LiveUnavailable(RuntimeError):
    """Configured Redis live store cannot be read."""


def close_quietly(client: redis.Redis | None) -> None:
    if client is None:
        return
    try:
        client.close()
    except Exception as exc:
        log.debug("Error closing Redis client: %s", exc)


class LiveConnection:
    """The shared Redis client, reconnected (throttled) when lost.

    Subclasses call :meth:`_connect` once their state is set up, and may
    override :meth:`_on_reconnect` and :meth:`_on_disconnect`.
    """

    def __init__(self, redis_uri: str) -> None:
        self.uri = redis_uri
        self._log_uri = redact_uri(redis_uri)
        # ``client`` is swapped by whichever thread notices a disconnect, so
        # methods take one local snapshot via :meth:`_redis` and use only that.
        self.client: redis.Redis | None = None
        # Guards the client fields only; never held across network I/O.
        self._conn_lock = threading.RLock()
        # Held by the one thread reconnecting; the others fail fast.
        self._reconnect_lock = threading.Lock()
        self._last_connect_attempt = 0.0
        self._last_ping_ok = 0.0
        self._reported_down = False

    def _on_reconnect(self, client: redis.Redis) -> None:
        """Runs on a reconnected client before it is published.

        A :class:`RedisError` aborts the reconnect (the client is dropped and
        the next attempt starts over).
        """

    def _on_disconnect(self) -> None:
        """Runs under ``_conn_lock`` when the shared client is dropped."""

    def _connect(self) -> None:
        """Create the Redis client, verify it, and publish it as ``self.client``.

        Safe to call repeatedly: on failure the store keeps running (the
        writer with its in-memory buffer) and retries later via
        :meth:`_redis`. The network round trips (up to the 2 s socket
        timeouts each) run without ``_conn_lock``, and the client is published
        only once usable: for the engine, after it re-claimed the heartbeat
        and backfilled Redis, so no other thread writes through it before
        ownership is settled.
        """
        client = redis.Redis.from_url(
            self.uri,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        reconnected = self._reported_down
        try:
            client.ping()
            if reconnected:
                self._on_reconnect(client)
        except RedisError as exc:
            close_quietly(client)
            self._report_down(exc)
            return
        with self._conn_lock:
            self.client = client
            self._last_ping_ok = time.monotonic()
            self._reported_down = False
        if reconnected:
            log.info("Reconnected to Redis at %s", self._log_uri)
        else:
            log.info("Connected to Redis at %s", self._log_uri)

    def _report_down(self, exc: BaseException | None) -> None:
        with self._conn_lock:
            if self._reported_down:
                return
            self._reported_down = True
        if exc is not None:
            log.warning(
                "Redis unavailable at %s; operating with in-memory live "
                "buffer. Reason: %s",
                self._log_uri,
                exc,
            )

    def _mark_disconnected(
        self,
        exc: BaseException | None = None,
        client: redis.Redis | None = None,
    ) -> None:
        """Drop the shared client after a failure.

        When *client* is given, only that instance is dropped: if another
        thread already reconnected, its fresh client is left alone.
        """
        with self._conn_lock:
            current = self.client
            if current is None or (client is not None and client is not current):
                return
            self.client = None
            self._on_disconnect()
        close_quietly(current)
        self._report_down(exc)

    def _redis(self) -> redis.Redis | None:
        """Return a connected client snapshot, or None when Redis is down.

        Reconnects automatically (throttled) so a Redis that comes up after
        startup, or recovers from an outage, is picked up without a restart.
        Only one thread reconnects at a time; the others get None at once
        instead of queueing behind its socket timeouts (portal readers would
        stall ~2 s per attempt while Redis is down).
        """
        with self._conn_lock:
            client = self.client
            now = time.monotonic()
            if client is not None:
                if now - self._last_ping_ok < _RECONNECT_DELAY:
                    return client
            elif now - self._last_connect_attempt < _RECONNECT_DELAY:
                return None
            elif not self._reconnect_lock.acquire(blocking=False):
                return None
            else:
                self._last_connect_attempt = now
        if client is None:
            try:
                self._connect()
            except Exception:  # pragma: no cover - defensive
                log.debug("Redis reconnect attempt failed", exc_info=True)
            finally:
                # Cool down from the end of a slow failed attempt, not its start.
                self._last_connect_attempt = time.monotonic()
                self._reconnect_lock.release()
            return self.client
        try:
            client.ping()
        except RedisError as exc:
            log.warning("Lost Redis connection; operating with in-memory live buffer.")
            self._mark_disconnected(exc, client)
            return None
        self._last_ping_ok = now
        return client

    def _ensure_connected(self) -> bool:
        return self._redis() is not None

    def ping(self) -> bool:
        """Return True if this store can serve reads.

        Ready when the server is reachable; a reconnect is attempted if the
        client is down.
        """
        return self._ensure_connected()

    def close(self) -> None:
        with self._conn_lock:
            client, self.client = self.client, None
        close_quietly(client)
