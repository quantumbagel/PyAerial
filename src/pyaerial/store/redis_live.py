"""Redis storage backend for active flight state, telemetry rings, and alert episodes."""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import redis
from redis.exceptions import RedisError

from pyaerial.constants import (
    ALERT_CAT_ETA,
    ALERT_CAT_REASON,
    ALERT_CAT_TYPE,
    ALERT_CAT_ZONE,
    LIVE_ENGINE_TTL_SECONDS,
    STORE_ALT,
    STORE_CALC_DATA,
    STORE_CALLSIGN,
    STORE_FIRST_PACKET,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_MOST_RECENT_PACKET,
    STORE_RECV_DATA,
)
from pyaerial.jsonutil import dumps as json_dumps
from pyaerial.models import Datum, flight_id_for_plane, iter_telemetry_samples
from pyaerial.store.memory import MemoryLiveBuffer
from pyaerial.store.present import live_flight_detail, live_flight_summary

log = logging.getLogger("pyaerial.store.redis")


class LiveUnavailable(RuntimeError):
    """Configured Redis live store cannot be read."""


def redact_uri(uri: str) -> str:
    """Return ``uri`` with any password replaced by ``***`` (safe to log)."""
    try:
        parts = urlsplit(uri)
    except ValueError:
        return "<unparseable redis uri>"
    if parts.password is None:
        return uri
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    user = parts.username or ""
    return urlunsplit(parts._replace(netloc=f"{user}:***@{host}"))


def _safe_json_loads(raw: Any) -> Any | None:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _engine_heartbeat_payload(token: str, seen_at: float | None = None) -> str:
    return json_dumps(
        {
            "seen_at": seen_at if seen_at is not None else time.time(),
            "token": token,
        }
    )


_KEY_FLIGHTS = "live:flights"
_KEY_FLIGHT = "live:flight:{flight_id}"
_KEY_TELEMETRY = "live:telemetry:{flight_id}"
_KEY_ALERTS = "live:alerts:{flight_id}"
_KEY_ACTIVE_ALERTS = "live:active_alerts"
_KEY_ALERT_EPISODES = "live:alert_episodes"
_KEY_ENGINE = "live:engine"
_RAW_CHANNEL = "live:raw"
_RECONNECT_DELAY = 2.0
_RAW_PUBSUB_RETRY = 2.0


class RedisLiveStore:
    """Shared in-memory live store for engine writes and web portal reads."""

    def __init__(
        self,
        redis_uri: str,
        *,
        memory_only: bool = False,
        telemetry_keep_seconds: float = 600.0,
        writer: bool = False,
    ):
        self.uri = redis_uri
        self._log_uri = redact_uri(redis_uri)
        self.memory_only = memory_only
        self.telemetry_keep_seconds = telemetry_keep_seconds
        # Only the tracking engine owns Redis live keys. Portal/CLI readers
        # reconnect without deleting flights that are missing from their empty mem.
        self.writer = writer
        # ``client`` is swapped by whichever thread notices a disconnect, so
        # methods take one local snapshot via :meth:`_redis` and use only that.
        self.client: redis.Redis | None = None
        self._conn_lock = threading.RLock()
        self._last_telemetry_ts: dict[str, float] = {}
        self._mem = MemoryLiveBuffer()
        self._last_connect_attempt = 0.0
        self._last_ping_ok = 0.0
        self._reported_down = False
        self._raw_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._raw_stop = threading.Event()
        self._raw_thread: threading.Thread | None = None
        self._raw_pubsub: Any = None
        self._raw_callback: Callable[[dict[str, Any]], None] | None = None
        self._pending_pops: set[str] = set()
        self._engine_token = uuid.uuid4().hex
        if memory_only:
            self._reported_down = True
            log.info("Live store running in memory-only mode (no Redis).")
        else:
            self._connect()

    def _connect(self) -> None:
        """Create the Redis client and verify connectivity.

        Safe to call repeatedly: on failure the store keeps running with the
        in-memory buffer and retries later via :meth:`_redis`.
        """
        with self._conn_lock:
            try:
                client = redis.Redis.from_url(
                    self.uri,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                self.client = client
                client.ping()
                self._last_ping_ok = time.monotonic()
                if self._reported_down:
                    log.info("Reconnected to Redis at %s", self._log_uri)
                    self._reported_down = False
                    if self.writer:
                        if not self.claim_engine():
                            self.writer = False
                            log.error(
                                "Reconnected to Redis but another engine holds "
                                "live:engine; yielding the live writer"
                            )
                        else:
                            self._backfill_redis_from_mem()
                else:
                    log.info("Connected to Redis at %s", self._log_uri)
                self._reported_down = False
            except RedisError as exc:
                self._mark_disconnected(exc)

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
            try:
                current.close()
            except Exception as close_exc:
                log.debug("Error closing Redis client: %s", close_exc)
            if not self._reported_down:
                self._reported_down = True
                if exc is not None:
                    log.warning(
                        "Redis unavailable at %s; operating with in-memory live "
                        "buffer. Reason: %s",
                        self._log_uri,
                        exc,
                    )

    def _redis(self) -> redis.Redis | None:
        """Return a connected client snapshot, or None to use the memory buffer.

        Reconnects automatically (throttled) so a Redis that comes up after
        startup, or recovers from an outage, is picked up without a restart.
        """
        if self.memory_only:
            return None
        with self._conn_lock:
            client = self.client
            now = time.monotonic()
            if client is None:
                if now - self._last_connect_attempt < _RECONNECT_DELAY:
                    return None
                self._last_connect_attempt = now
                try:
                    self._connect()
                except Exception:  # pragma: no cover - defensive
                    log.debug("Redis reconnect attempt failed", exc_info=True)
                return self.client
            if now - self._last_ping_ok < _RECONNECT_DELAY:
                return client
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

    def _reader_needs_redis(self) -> bool:
        return not self.writer and not self.memory_only

    def _raise_if_reader_disconnected(self) -> None:
        if self._reader_needs_redis():
            raise LiveUnavailable("live store is unavailable")

    def ping(self) -> bool:
        """Return True if this store can serve reads.

        Memory-only mode is always ready. Redis mode is ready when the
        server is reachable; a reconnect is attempted if the client is down.
        """
        if self.memory_only:
            return True
        return self._ensure_connected()

    def _owns_heartbeat(self, doc: Any) -> bool:
        return isinstance(doc, dict) and doc.get("token") == self._engine_token

    def _heartbeat_is_fresh(self, doc: Any, now: float | None = None) -> bool:
        if not isinstance(doc, dict):
            return False
        seen = doc.get("seen_at")
        if not isinstance(seen, (int, float)):
            return False
        stamp = now if now is not None else time.time()
        return stamp - seen < LIVE_ENGINE_TTL_SECONDS

    def touch_engine(self) -> None:
        """Record that the tracking engine is alive.

        Written every engine tick, including when no aircraft are tracked, so
        the portal can tell "engine down" from "no traffic." Refreshes only
        this process's token; a foreign live heartbeat is left alone.
        """
        now = time.time()
        self._mem.engine_seen_at = now
        client = self._redis()
        if client is None:
            return
        payload = _engine_heartbeat_payload(self._engine_token, now)
        try:
            raw = client.get(_KEY_ENGINE)
            doc = _safe_json_loads(raw) if raw else None
            if (
                raw
                and not self._owns_heartbeat(doc)
                and self._heartbeat_is_fresh(doc, now)
            ):
                return
            client.set(_KEY_ENGINE, payload, ex=LIVE_ENGINE_TTL_SECONDS)
        except RedisError as exc:
            log.error("Failed to write engine heartbeat: %s", exc)
            self._mark_disconnected(exc, client)

    def engine_seen_at(self) -> float | None:
        """Unix timestamp of the last engine heartbeat, or None if missing."""
        client = self._redis()
        if client is not None:
            try:
                raw = client.get(_KEY_ENGINE)
                if raw:
                    doc = json.loads(raw)
                    seen = doc.get("seen_at") if isinstance(doc, dict) else None
                    if isinstance(seen, (int, float)):
                        return float(seen)
            except (RedisError, json.JSONDecodeError, TypeError, ValueError) as exc:
                log.debug("Could not read engine heartbeat: %s", exc)
        return self._mem.engine_seen_at

    def engine_is_live(self) -> bool:
        """True when a fresh ``live:engine`` heartbeat exists."""
        seen = self.engine_seen_at()
        if not isinstance(seen, (int, float)):
            return False
        return time.time() - seen < LIVE_ENGINE_TTL_SECONDS

    def other_engine_is_live(self) -> bool:
        """True when another process holds a fresh live:engine heartbeat."""
        if self.memory_only:
            return False
        client = self._redis()
        if client is None:
            return False
        try:
            raw = client.get(_KEY_ENGINE)
        except RedisError as exc:
            log.debug("Could not read engine heartbeat: %s", exc)
            return False
        doc = _safe_json_loads(raw) if raw else None
        if self._owns_heartbeat(doc):
            return False
        return self._heartbeat_is_fresh(doc)

    def ensure_writer(self) -> bool:
        """True if this process may write live Redis keys.

        Redis down: keep tracking in memory. Once Redis is back, reclaim the
        heartbeat or yield if another engine holds a fresh token.
        """
        if self.memory_only:
            return True
        if not self.writer:
            return False
        if self._redis() is None:
            return True
        if self.other_engine_is_live() or not self.claim_engine():
            self.writer = False
            log.error(
                "Another tracking engine holds a fresh live:engine heartbeat; "
                "yielding the live writer"
            )
            return False
        return True

    def claim_engine(self) -> bool:
        """Take ownership of the live writer heartbeat, or refuse if another engine is up.

        Uses SET NX plus a per-process token so Docker PID namespaces cannot
        mistake a still-running peer for a dead local pid.
        """
        if self.memory_only:
            self.touch_engine()
            return True
        now = time.time()
        self._mem.engine_seen_at = now
        client = self._redis()
        if client is None:
            return False
        payload = _engine_heartbeat_payload(self._engine_token, now)
        try:
            if client.set(_KEY_ENGINE, payload, ex=LIVE_ENGINE_TTL_SECONDS, nx=True):
                return True
            raw = client.get(_KEY_ENGINE)
            doc = _safe_json_loads(raw) if raw else None
            if self._owns_heartbeat(doc):
                client.set(_KEY_ENGINE, payload, ex=LIVE_ENGINE_TTL_SECONDS)
                return True
            if self._heartbeat_is_fresh(doc, now):
                return False
            client.set(_KEY_ENGINE, payload, ex=LIVE_ENGINE_TTL_SECONDS)
            return True
        except RedisError as exc:
            log.error("Failed to claim engine heartbeat: %s", exc)
            return False

    def clear_engine(self) -> None:
        """Drop this process's engine heartbeat so readers see it as stopped.

        A foreign live token is left alone so a yielding loser cannot drop
        the winner's heartbeat.
        """
        self._mem.engine_seen_at = None
        client = self._redis()
        if client is None:
            return
        try:
            raw = client.get(_KEY_ENGINE)
            doc = _safe_json_loads(raw) if raw else None
            if raw and not self._owns_heartbeat(doc):
                return
            client.delete(_KEY_ENGINE)
        except RedisError as exc:
            log.debug("Could not clear engine heartbeat: %s", exc)

    def clear_all(self) -> None:
        """Remove stale live keys from a previous engine session."""
        self._mem.clear()
        client = self._redis()
        if client is None:
            return
        try:
            flight_ids = list(client.smembers(_KEY_FLIGHTS))
            pipe = client.pipeline()
            for flight_id in flight_ids:
                pipe.delete(
                    _KEY_FLIGHT.format(flight_id=flight_id),
                    _KEY_TELEMETRY.format(flight_id=flight_id),
                    _KEY_ALERTS.format(flight_id=flight_id),
                )
            pipe.delete(
                _KEY_FLIGHTS, _KEY_ACTIVE_ALERTS, _KEY_ALERT_EPISODES, _KEY_ENGINE
            )
            pipe.execute()
            self._pending_pops.clear()
            log.info("Cleared %d stale live flight(s) from Redis.", len(flight_ids))
        except RedisError as exc:
            log.warning("Could not clear Redis live store: %s", exc)

    def write_live_planes(self, planes: dict[str, dict]) -> None:
        if not planes:
            return
        if not self.writer and not self.memory_only:
            return
        for plane in planes.values():
            self._upsert_live_flight(plane)

    def record_alert_episode(
        self,
        plane: dict,
        meta: dict[str, Any],
        payload: dict[str, Any],
        *,
        alert_id: str,
        activated_at: float,
        active: bool = True,
        deactivated_at: float | None = None,
    ) -> None:
        """Record alert activation/deactivation and update the live active set.

        On activation (active=True) or deactivation (active=False), the existing
        document for this alert_id is updated in place so there is never more than
        one document per alert episode.
        """
        flight_id = flight_id_for_plane(plane)
        doc = self._alert_doc(
            plane,
            meta,
            payload,
            alert_id=alert_id,
            activated_at=activated_at,
            active=active,
            deactivated_at=deactivated_at,
        )
        if active:
            self._mem.active_alerts[alert_id] = doc
        else:
            self._mem.active_alerts.pop(alert_id, None)

        flight_alerts = self._mem.alerts[flight_id]
        for i, existing in enumerate(flight_alerts):
            if existing.get("alert_id") == alert_id:
                flight_alerts[i] = doc
                break
        else:
            flight_alerts.append(doc)

        for i, existing in enumerate(self._mem.alert_episodes):
            if existing.get("alert_id") == alert_id:
                self._mem.alert_episodes[i] = doc
                break
        else:
            self._mem.alert_episodes.insert(0, doc)

        if not self.writer:
            return
        client = self._redis()
        if client is None:
            return
        encoded = json_dumps(doc)
        try:
            pipe = client.pipeline()
            alerts_key = _KEY_ALERTS.format(flight_id=flight_id)
            if active:
                pipe.hset(_KEY_ACTIVE_ALERTS, alert_id, encoded)
            else:
                pipe.hdel(_KEY_ACTIVE_ALERTS, alert_id)
            # Both per-flight alerts and the shared episode index are hashes
            # keyed by alert_id, so upserts are O(1) instead of a list scan.
            pipe.hset(alerts_key, alert_id, encoded)
            pipe.hset(_KEY_ALERT_EPISODES, alert_id, encoded)
            pipe.execute()
        except RedisError as exc:
            log.error("Failed to record alert episode for %s: %s", flight_id, exc)
            # Without this the reconnect backfill never runs and a failed
            # deactivation leaves a ghost active alert in Redis.
            self._mark_disconnected(exc, client)

    def update_active_alert(
        self,
        plane: dict,
        alert_id: str,
        meta: dict[str, Any],
        payload: dict[str, Any],
        timestamp: float,
    ) -> None:
        """Refresh ETA, position, and telemetry on an active alert."""
        existing = self._mem.active_alerts.get(alert_id)
        activated_at = (
            existing.get("activated_at", timestamp) if existing else timestamp
        )
        if existing is not None:
            doc = dict(existing)
            doc.update(
                self._alert_doc(
                    plane,
                    meta,
                    payload,
                    alert_id=alert_id,
                    activated_at=activated_at,
                    active=True,
                    deactivated_at=None,
                )
            )
        else:
            doc = self._alert_doc(
                plane,
                meta,
                payload,
                alert_id=alert_id,
                activated_at=activated_at,
                active=True,
                deactivated_at=None,
            )
        doc["last_updated"] = timestamp
        self._mem.active_alerts[alert_id] = doc

        flight_id = flight_id_for_plane(plane)
        flight_alerts = self._mem.alerts.get(flight_id, [])
        for i, item in enumerate(flight_alerts):
            if item.get("alert_id") == alert_id:
                flight_alerts[i] = doc
                break

        for i, item in enumerate(self._mem.alert_episodes):
            if item.get("alert_id") == alert_id:
                self._mem.alert_episodes[i] = doc
                break

        if not self.writer:
            return
        client = self._redis()
        if client is None:
            return
        try:
            raw = client.hget(_KEY_ACTIVE_ALERTS, alert_id)
            if raw:
                stored = _safe_json_loads(raw)
                if isinstance(stored, dict):
                    doc["activated_at"] = stored.get("activated_at", activated_at)
            pipe = client.pipeline()
            pipe.hset(_KEY_ACTIVE_ALERTS, alert_id, json_dumps(doc))
            pipe.hset(
                _KEY_ALERTS.format(flight_id=flight_id),
                alert_id,
                json_dumps(doc),
            )
            pipe.hset(_KEY_ALERT_EPISODES, alert_id, json_dumps(doc))
            pipe.execute()
        except RedisError as exc:
            log.error("Failed to update active alert %s: %s", alert_id, exc)
            self._mark_disconnected(exc, client)

    def _alert_doc(
        self,
        plane: dict,
        meta: dict[str, Any],
        payload: dict[str, Any],
        *,
        alert_id: str,
        activated_at: float,
        active: bool,
        deactivated_at: float | None = None,
    ) -> dict[str, Any]:
        flight_id = flight_id_for_plane(plane)
        return {
            "alert_id": alert_id,
            "flight_id": flight_id,
            "icao": meta[STORE_ICAO].lower(),
            "callsign": meta.get(STORE_CALLSIGN) or "",
            "zone": meta.get(ALERT_CAT_ZONE, ""),
            "rule": meta.get(ALERT_CAT_TYPE, ""),
            "active": active,
            "activated_at": activated_at,
            "deactivated_at": deactivated_at,
            "eta": meta.get(ALERT_CAT_ETA),
            "reason": meta.get(ALERT_CAT_REASON),
            "last_updated": deactivated_at or activated_at,
            "position": {
                "type": "Point",
                "coordinates": [payload.get(STORE_LONG), payload.get(STORE_LAT)],
            },
            "altitude": payload.get(STORE_ALT),
        }

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
            results.sort(key=lambda item: item.get("start_time") or 0, reverse=True)
            return results
        results = []
        try:
            flight_ids = sorted(client.smembers(_KEY_FLIGHTS))
            if flight_ids:
                # One round trip for every flight doc and its newest telemetry
                # point (was 2 round trips per flight).
                pipe = client.pipeline(transaction=False)
                for flight_id in flight_ids:
                    pipe.get(_KEY_FLIGHT.format(flight_id=flight_id))
                    pipe.zrevrange(_KEY_TELEMETRY.format(flight_id=flight_id), 0, 0)
                replies = pipe.execute()
                for index in range(len(flight_ids)):
                    raw, last_raw = replies[2 * index], replies[2 * index + 1]
                    doc = _safe_json_loads(raw) if raw else None
                    if not isinstance(doc, dict):
                        continue
                    last_tel = _safe_json_loads(last_raw[0]) if last_raw else None
                    results.append(
                        live_flight_summary(
                            doc, last_tel if isinstance(last_tel, dict) else None
                        )
                    )
        except RedisError as exc:
            log.error("Failed to read live flights from Redis: %s", exc)
            self._mark_disconnected(exc, client)
            self._raise_if_reader_disconnected()
        results.sort(key=lambda item: item.get("start_time") or 0, reverse=True)
        return results

    def count_flights(self) -> int:
        """Number of live flights (SCARD; no documents are fetched)."""
        client = self._redis()
        if client is None:
            self._raise_if_reader_disconnected()
            return len(self._mem.flights)
        try:
            return int(client.scard(_KEY_FLIGHTS))
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
            raw = client.get(_KEY_FLIGHT.format(flight_id=flight_id))
            if not raw:
                return None
            doc = _safe_json_loads(raw)
            if not isinstance(doc, dict):
                return None
            return live_flight_detail(doc, flight_id)
        except RedisError as exc:
            log.error("Failed to read live flight %s: %s", flight_id, exc)
            self._mark_disconnected(exc, client)
            self._raise_if_reader_disconnected()
            return None

    def get_telemetry(
        self, flight_id: str, *, since: float = 0.0
    ) -> list[dict[str, Any]]:
        client = self._redis()
        if client is None:
            self._raise_if_reader_disconnected()
            points = self._mem.telemetry.get(flight_id, [])
            if since > 0:
                points = [p for p in points if p.get("timestamp", 0) > since]
            return points
        try:
            return self._read_telemetry(client, flight_id, since)
        except RedisError as exc:
            log.error("Failed to read telemetry for %s: %s", flight_id, exc)
            self._mark_disconnected(exc, client)
            self._raise_if_reader_disconnected()
            return []

    @staticmethod
    def _read_telemetry(
        client: redis.Redis, flight_id: str, since: float
    ) -> list[dict[str, Any]]:
        key = _KEY_TELEMETRY.format(flight_id=flight_id)
        if since > 0:
            raw_points = client.zrangebyscore(key, f"({since}", "+inf")
        else:
            raw_points = client.zrange(key, 0, -1)
        points = []
        for point in raw_points:
            parsed = _safe_json_loads(point)
            if isinstance(parsed, dict):
                points.append(parsed)
        return points

    @staticmethod
    def _live_point(flight_id: str, point: dict[str, Any]) -> dict[str, Any]:
        return {
            "flight_id": flight_id,
            "icao": point.get("icao"),
            "timestamp": point.get("timestamp"),
            "latitude": point.get("latitude"),
            "longitude": point.get("longitude"),
            "altitude": point.get("altitude"),
            "speed": point.get("speed"),
            "heading": point.get("heading"),
        }

    def get_live_telemetry(self, since: float = 0.0) -> list[dict[str, Any]]:
        client = self._redis()
        points: list[dict[str, Any]] = []
        if client is None:
            self._raise_if_reader_disconnected()
            for flight_id in self._mem.flights:
                for point in self._mem.telemetry.get(flight_id, []):
                    if since > 0 and point.get("timestamp", 0) <= since:
                        continue
                    points.append(self._live_point(flight_id, point))
        else:
            try:
                for flight_id in client.smembers(_KEY_FLIGHTS):
                    for point in self._read_telemetry(client, flight_id, since):
                        points.append(self._live_point(flight_id, point))
            except RedisError as exc:
                log.error("Failed to read live telemetry: %s", exc)
                self._mark_disconnected(exc, client)
                self._raise_if_reader_disconnected()
        points.sort(key=lambda item: item.get("timestamp") or 0)
        return points

    def get_alerts(
        self,
        *,
        since: float = 0.0,
        flight_id: str | None = None,
        rule: str | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        mem_has_flight = bool(flight_id) and flight_id in self._mem.alerts
        client = None if mem_has_flight else self._redis()
        if client is None:
            if not mem_has_flight:
                self._raise_if_reader_disconnected()
            alerts = self._mem_alerts(flight_id, active_only)
        else:
            try:
                alerts = self._redis_alerts(client, flight_id, active_only)
            except RedisError as exc:
                log.error("Failed to read live alerts: %s", exc)
                self._mark_disconnected(exc, client)
                self._raise_if_reader_disconnected()
                return []
        if since:
            alerts = [
                a
                for a in alerts
                if (a.get("activated_at") or a.get("last_updated") or 0) >= since
            ]
        if rule:
            alerts = [a for a in alerts if a.get("rule") == rule]
        if active_only:
            alerts = [a for a in alerts if a.get("active", True)]
        alerts.sort(
            key=lambda item: item.get("activated_at") or item.get("last_updated") or 0,
            reverse=True,
        )
        return alerts

    def _mem_alerts(
        self, flight_id: str | None, active_only: bool
    ) -> list[dict[str, Any]]:
        if active_only:
            return [
                a
                for a in self._mem.active_alerts.values()
                if not flight_id or a.get("flight_id") == flight_id
            ]
        if flight_id:
            return list(self._mem.alerts.get(flight_id, []))
        return list(self._mem.alert_episodes)

    @staticmethod
    def _redis_alerts(
        client: redis.Redis, flight_id: str | None, active_only: bool
    ) -> list[dict[str, Any]]:
        if active_only:
            raw_alerts = client.hvals(_KEY_ACTIVE_ALERTS)
        elif flight_id:
            raw_alerts = client.hvals(_KEY_ALERTS.format(flight_id=flight_id))
        else:
            raw_alerts = client.hvals(_KEY_ALERT_EPISODES)
        alerts = []
        for raw in raw_alerts:
            parsed = _safe_json_loads(raw)
            if not isinstance(parsed, dict):
                continue
            if active_only and flight_id and parsed.get("flight_id") != flight_id:
                continue
            alerts.append(parsed)
        return alerts

    def pop_flight(self, flight_id: str) -> dict[str, Any]:
        """Return buffered flight data and delete Redis keys for the flight."""
        mem_flight = self._mem.flights.pop(flight_id, None)
        mem_alerts = self._mem.alerts.pop(flight_id, [])
        self._mem.telemetry.pop(flight_id, None)
        self._last_telemetry_ts.pop(flight_id, None)
        self._mem.alert_episodes = [
            a for a in self._mem.alert_episodes if a.get("flight_id") != flight_id
        ]
        for a in list(self._mem.active_alerts.values()):
            if a.get("flight_id") == flight_id:
                self._mem.active_alerts.pop(a["alert_id"], None)

        client = self._redis()
        if client is None:
            self._pending_pops.add(flight_id)
            return {
                "flight": mem_flight,
                "alerts": mem_alerts,
            }
        try:
            snapshot = self._delete_redis_flight(client, flight_id)
            self._pending_pops.discard(flight_id)
            return {
                "flight": snapshot.get("flight") or mem_flight,
                "alerts": snapshot.get("alerts") or mem_alerts,
            }
        except RedisError as exc:
            log.error("Failed to pop live flight %s: %s", flight_id, exc)
            self._pending_pops.add(flight_id)
            self._mark_disconnected(exc, client)
            return {"flight": mem_flight, "alerts": mem_alerts}

    def retry_pending_pops(self) -> None:
        if not self.writer or not self._pending_pops:
            return
        client = self._redis()
        if client is None:
            return
        for flight_id in list(self._pending_pops):
            try:
                self._delete_redis_flight(client, flight_id)
                self._pending_pops.discard(flight_id)
            except RedisError as exc:
                self._mark_disconnected(exc, client)
                return

    def flight_ids(self) -> set[str]:
        client = self._redis()
        if client is not None:
            try:
                return set(client.smembers(_KEY_FLIGHTS))
            except RedisError as exc:
                self._mark_disconnected(exc, client)
        return set(self._mem.flights)

    def planes_for_finalize(self) -> list[dict[str, Any]]:
        planes = []
        for flight_id in self.flight_ids():
            plane = self.plane_for_finalize(flight_id)
            if plane is not None:
                planes.append(plane)
        return planes

    def plane_for_finalize(self, flight_id: str) -> dict[str, Any] | None:
        doc: dict[str, Any] | None = self._mem.flights.get(flight_id)
        client = self._redis() if doc is None else None
        if client is not None:
            try:
                raw = client.get(_KEY_FLIGHT.format(flight_id=flight_id))
            except RedisError as exc:
                self._mark_disconnected(exc, client)
                raw = None
            parsed = _safe_json_loads(raw) if raw else None
            doc = parsed if isinstance(parsed, dict) else None
        if not doc:
            return None
        info = dict(doc.get("info") or {})
        icao = str(doc.get("icao") or info.get(STORE_ICAO) or "").lower()
        if not icao:
            return None
        info[STORE_ICAO] = icao
        points = self.get_telemetry(flight_id)
        recv: dict[str, list] = {}
        calc: dict[str, list] = {}

        def _append(bucket: dict[str, list], key: str, value: Any, ts: float) -> None:
            if value is None:
                return
            bucket.setdefault(key, []).append(Datum(value, ts))

        for point in points:
            ts = point.get("timestamp")
            if not isinstance(ts, (int, float)):
                continue
            _append(recv, STORE_LAT, point.get("latitude"), ts)
            _append(recv, STORE_LONG, point.get("longitude"), ts)
            _append(recv, STORE_ALT, point.get("altitude"), ts)
            _append(calc, STORE_HORIZ_SPEED, point.get("speed"), ts)
            _append(calc, STORE_HEADING, point.get("heading"), ts)
        start = doc.get("start_time")
        end = doc.get("end_time")
        if not isinstance(start, (int, float)):
            start = points[0]["timestamp"] if points else time.time()
        if not isinstance(end, (int, float)):
            end = points[-1]["timestamp"] if points else start
        return {
            STORE_INFO: info,
            STORE_INTERNAL: {
                STORE_FIRST_PACKET: start,
                STORE_MOST_RECENT_PACKET: end,
            },
            STORE_RECV_DATA: recv,
            STORE_CALC_DATA: calc,
            "active_alerts": doc.get("active_alerts") or [],
        }

    @staticmethod
    def _delete_redis_flight(client: redis.Redis, flight_id: str) -> dict[str, Any]:
        raw_flight = client.get(_KEY_FLIGHT.format(flight_id=flight_id))
        flight_alerts_raw = client.hgetall(_KEY_ALERTS.format(flight_id=flight_id))
        raw_alerts = list(flight_alerts_raw.values())
        active_raw = []
        for alert_id, raw in client.hgetall(_KEY_ACTIVE_ALERTS).items():
            parsed = _safe_json_loads(raw)
            if isinstance(parsed, dict) and parsed.get("flight_id") == flight_id:
                active_raw.append(alert_id)
        pipe = client.pipeline()
        pipe.srem(_KEY_FLIGHTS, flight_id)
        pipe.delete(
            _KEY_FLIGHT.format(flight_id=flight_id),
            _KEY_TELEMETRY.format(flight_id=flight_id),
            _KEY_ALERTS.format(flight_id=flight_id),
        )
        for alert_id in active_raw:
            pipe.hdel(_KEY_ACTIVE_ALERTS, alert_id)
        if flight_alerts_raw:
            pipe.hdel(_KEY_ALERT_EPISODES, *flight_alerts_raw.keys())
        pipe.execute()
        flight_doc = None
        if raw_flight:
            try:
                flight_doc = json.loads(raw_flight)
            except (json.JSONDecodeError, TypeError, ValueError):
                flight_doc = None
        alerts = []
        for raw in raw_alerts:
            try:
                alerts.append(json.loads(raw))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return {"flight": flight_doc, "alerts": alerts}

    def publish_raw(self, messages: list[dict[str, Any]]) -> None:
        """Publish a batch of raw sensor frames to local listeners and Redis.

        The web app subscribes to ``live:raw`` and re-encodes batches as
        dump1090 Beast binary for ``/ws/beast`` and the optional Beast TCP
        port. Empty batches are ignored.
        """
        if not messages:
            return
        payload: dict[str, Any] = {
            "timestamp": time.time(),
            "messages": messages,
        }
        for listener in list(self._raw_listeners):
            try:
                listener(payload)
            except Exception:
                log.debug("Raw-frame listener failed", exc_info=True)
        client = self._redis()
        if client is None:
            return
        try:
            client.publish(_RAW_CHANNEL, json_dumps(payload))
        except RedisError as exc:
            log.debug("Failed to publish raw frames: %s", exc)

    def add_raw_listener(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Register an in-process listener for :meth:`publish_raw`."""
        self._raw_listeners.append(callback)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._raw_listeners.remove(callback)

        return remove

    def start_raw_pubsub(self, on_payload: Callable[[dict[str, Any]], None]) -> None:
        """Listen for ``live:raw`` on Redis and invoke *on_payload*.

        Always registers an in-process listener so tests (memory-only) and a
        co-located engine see local publishes. A background thread subscribes
        to Redis when this store is not memory-only.
        """
        if self._raw_callback is on_payload:
            return
        if self._raw_callback is not None:
            self.stop_raw_pubsub()
        self._raw_callback = on_payload
        self.add_raw_listener(on_payload)
        if self.memory_only:
            return
        self._raw_stop.clear()
        self._raw_thread = threading.Thread(
            target=self._raw_pubsub_loop,
            name="pyaerial-raw-pubsub",
            daemon=True,
        )
        self._raw_thread.start()

    def stop_raw_pubsub(self) -> None:
        callback = self._raw_callback
        self._raw_callback = None
        if callback is not None:
            with contextlib.suppress(ValueError):
                self._raw_listeners.remove(callback)
        self._raw_stop.set()
        pubsub = self._raw_pubsub
        self._raw_pubsub = None
        if pubsub is not None:
            try:
                pubsub.close()
            except Exception as exc:
                log.debug("Error closing raw pub/sub: %s", exc)
        thread = self._raw_thread
        self._raw_thread = None
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)

    def _raw_pubsub_loop(self) -> None:
        while not self._raw_stop.is_set():
            client: redis.Redis | None = None
            pubsub = None
            try:
                client = redis.Redis.from_url(
                    self.uri,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(_RAW_CHANNEL)
                self._raw_pubsub = pubsub
                while not self._raw_stop.is_set():
                    message = pubsub.get_message(timeout=1.0)
                    if not message or message.get("type") != "message":
                        continue
                    data = message.get("data")
                    try:
                        payload = json.loads(data)
                    except (TypeError, json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(payload, dict) and self._raw_callback is not None:
                        self._raw_callback(payload)
            except Exception:
                log.debug("Raw pub/sub listener reconnecting", exc_info=True)
            finally:
                self._raw_pubsub = None
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception as exc:
                        log.debug("Error closing raw pub/sub: %s", exc)
                if client is not None:
                    try:
                        client.close()
                    except Exception as exc:
                        log.debug("Error closing raw pub/sub client: %s", exc)
            if self._raw_stop.wait(_RAW_PUBSUB_RETRY):
                return

    def close(self) -> None:
        self.stop_raw_pubsub()
        with self._conn_lock:
            client, self.client = self.client, None
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                log.debug("Error closing Redis client: %s", exc)

    def _upsert_live_flight(self, plane: dict) -> None:
        info = plane.get(STORE_INFO, {})
        internal = plane.get(STORE_INTERNAL, {})
        flight_id = flight_id_for_plane(plane)
        icao = info[STORE_ICAO].lower()
        first_packet = internal[STORE_FIRST_PACKET]
        last_packet = internal[STORE_MOST_RECENT_PACKET]

        flight_doc = {
            "flight_id": flight_id,
            "icao": icao,
            "status": "live",
            "active_alerts": plane.get("active_alerts") or [],
            "start_time": first_packet,
            "end_time": last_packet,
            "callsign": info.get(STORE_CALLSIGN) or "",
            "model": info.get("model") or "",
            "owner": info.get("owner") or "",
            "country": info.get("country") or "",
            "aircraft_type": info.get("aircraft_type") or "",
            "registration": info.get("registration") or "",
            "info": {str(k): v for k, v in dict(info).items()},
        }
        self._mem.flights[flight_id] = flight_doc
        self._write_telemetry_points(plane, flight_id, icao)

        if not self.writer:
            return
        client = self._redis()
        if client is None:
            return
        encoded = json_dumps(flight_doc)
        try:
            pipe = client.pipeline()
            pipe.sadd(_KEY_FLIGHTS, flight_id)
            pipe.set(_KEY_FLIGHT.format(flight_id=flight_id), encoded)
            pipe.execute()
        except RedisError as exc:
            log.error("Failed to upsert live flight %s: %s", flight_id, exc)
            self._mark_disconnected(exc, client)

    def _write_telemetry_points(self, plane: dict, flight_id: str, icao: str) -> None:
        last_written = self._last_telemetry_ts.get(flight_id, 0.0)
        samples = list(iter_telemetry_samples(plane, since=last_written))
        if not samples:
            return

        client = self._redis() if self.writer else None
        pipe = client.pipeline() if client is not None else None
        key = _KEY_TELEMETRY.format(flight_id=flight_id) if pipe else None

        for timestamp, lat, lon, alt, speed, heading in samples:
            point: dict[str, Any] = {
                "icao": icao,
                "timestamp": timestamp,
                "latitude": lat,
                "longitude": lon,
            }
            if alt is not None:
                point["altitude"] = alt
            if speed is not None:
                point["speed"] = speed
            if heading is not None:
                point["heading"] = heading

            self._mem.telemetry[flight_id].append(point)
            if pipe and key:
                pipe.zadd(key, {json_dumps(point): timestamp})
            last_written = max(last_written, timestamp)

        # Advance the cursor after the in-memory write so a failed Redis
        # execute does not duplicate mem points. Redis holes are repaired
        # by ``_backfill_redis_from_mem`` on reconnect.
        self._last_telemetry_ts[flight_id] = last_written
        keep = self.telemetry_keep_seconds
        if keep > 0:
            cutoff = time.time() - keep
            points = self._mem.telemetry[flight_id]
            trimmed = [point for point in points if point["timestamp"] >= cutoff]
            self._mem.telemetry[flight_id] = trimmed or points[-1:]
            if pipe and key:
                pipe.zremrangebyscore(key, "-inf", cutoff)
                kept = self._mem.telemetry[flight_id]
                if kept:
                    last_point = kept[-1]
                    last_ts = last_point.get("timestamp")
                    if isinstance(last_ts, (int, float)) and last_ts < cutoff:
                        pipe.zadd(key, {json_dumps(last_point): last_ts})
        if pipe:
            try:
                pipe.execute()
            except RedisError as exc:
                log.error("Failed to write live telemetry for %s: %s", flight_id, exc)
                self._mark_disconnected(exc, client)

    def _backfill_redis_from_mem(self) -> None:
        """Replay in-memory flights/telemetry/alerts after a Redis reconnect."""
        client = self.client
        if not self.writer or client is None:
            return
        try:
            # Only delete keys this process already decided to pop. Redis ids
            # that are not in mem may be leftovers from a previous engine and
            # must be archived by the engine, not wiped here.
            for flight_id in list(self._pending_pops):
                try:
                    self._delete_redis_flight(client, flight_id)
                    self._pending_pops.discard(flight_id)
                except RedisError as exc:
                    log.debug(
                        "Could not drop pending Redis flight %s: %s", flight_id, exc
                    )
            # Writes that failed while Redis was flaky (e.g. a deactivation's
            # HDEL) leave ghost active alerts behind. Drop any active alert of a
            # flight this engine owns that memory no longer considers active.
            owned = set(self._mem.flights)
            ghosts = []
            for alert_id, raw in client.hgetall(_KEY_ACTIVE_ALERTS).items():
                if alert_id in self._mem.active_alerts:
                    continue
                parsed = _safe_json_loads(raw)
                if isinstance(parsed, dict) and parsed.get("flight_id") in owned:
                    ghosts.append(alert_id)
            pipe = client.pipeline()
            if ghosts:
                pipe.hdel(_KEY_ACTIVE_ALERTS, *ghosts)
            for flight_id, doc in self._mem.flights.items():
                pipe.sadd(_KEY_FLIGHTS, flight_id)
                pipe.set(
                    _KEY_FLIGHT.format(flight_id=flight_id),
                    json_dumps(doc),
                )
                key = _KEY_TELEMETRY.format(flight_id=flight_id)
                for point in self._mem.telemetry.get(flight_id, []):
                    ts = point.get("timestamp")
                    if ts is None:
                        continue
                    pipe.zadd(key, {json_dumps(point): ts})
            for alert_id, doc in self._mem.active_alerts.items():
                encoded = json_dumps(doc)
                pipe.hset(_KEY_ACTIVE_ALERTS, alert_id, encoded)
            for doc in self._mem.alert_episodes:
                alert_id = doc.get("alert_id")
                flight_id = doc.get("flight_id")
                if not alert_id:
                    continue
                encoded = json_dumps(doc)
                pipe.hset(_KEY_ALERT_EPISODES, alert_id, encoded)
                if flight_id:
                    pipe.hset(
                        _KEY_ALERTS.format(flight_id=flight_id), alert_id, encoded
                    )
            pipe.execute()
            self.touch_engine()
        except RedisError as exc:
            log.error("Failed to backfill Redis from memory: %s", exc)
            self._mark_disconnected(exc, client)
