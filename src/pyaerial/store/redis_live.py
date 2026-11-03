"""Redis storage backend for active flight state, telemetry rings, and alert episodes."""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any, cast

import redis
from redis.exceptions import RedisError

from pyaerial.constants import (
    ALERT_CAT_ETA,
    ALERT_CAT_REASON,
    ALERT_CAT_TYPE,
    ALERT_CAT_ZONE,
    LIVE_ENGINE_TTL_SECONDS,
    STORE_ALT,
    STORE_CALLSIGN,
    STORE_ICAO,
    STORE_LAT,
    STORE_LONG,
)
from pyaerial.jsonutil import dumps as json_dumps
from pyaerial.models import Plane, iter_telemetry_samples
from pyaerial.store.codec import (
    live_doc_icao,
    live_flight_doc,
    live_telemetry_point,
    plane_from_live,
)
from pyaerial.store.memory import MemoryLiveBuffer
from pyaerial.store.present import live_flight_detail, live_flight_summary
from pyaerial.urls import redact_uri

log = logging.getLogger("pyaerial.store.redis")


class LiveUnavailable(RuntimeError):
    """Configured Redis live store cannot be read."""


def _safe_json_loads(raw: Any) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _close_quietly(client: redis.Redis | None) -> None:
    if client is None:
        return
    try:
        client.close()
    except Exception as exc:
        log.debug("Error closing Redis client: %s", exc)


def _engine_heartbeat_payload(
    token: str, seen_at: float | None = None, extra: dict[str, Any] | None = None
) -> str:
    return json_dumps(
        {
            **(extra or {}),
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
# Safety net on per-flight keys (doc, telemetry, alerts), re-armed on every
# write. While an engine runs it pops finalized flights itself, so this only
# matters after a crash: the next engine archives the leftovers from these
# keys, so they must outlive any downtime we still want to recover from (a
# crash on Friday night found on Monday morning, plus slack). Past it the
# flight is lost from the archive rather than kept in Redis memory forever.
# Shared indexes (live:flights, alert hashes) cannot expire per member; the
# next engine drops entries whose keys are gone.
LIVE_FLIGHT_TTL_SECONDS = 7 * 86_400
# Telemetry point fields the /ws/live push carries only when present.
_LIVE_POINT_OPTIONAL = (
    "gnss_height",
    "receivers",
    "integrity_score",
    "integrity_status",
    "integrity_flags",
)
# Unreadable live flight docs remembered per process, so the orphan sweep does
# not re-read (and re-log) them every tick.
_UNREADABLE_FLIGHTS_MAX = 1_024


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
        # Guards the client fields only; never held across network I/O.
        self._conn_lock = threading.RLock()
        # Held by the one thread reconnecting; the others fail fast.
        self._reconnect_lock = threading.Lock()
        self._last_telemetry_ts: dict[str, float] = {}
        # flight_id -> JSON last SET in Redis, so unchanged flights are skipped.
        self._written_docs: dict[str, str] = {}
        self._mem = MemoryLiveBuffer()
        # Held while changing ``_mem`` and while copying it for the reconnect
        # backfill (which may run on another thread). Never held across I/O.
        self._mem_lock = threading.RLock()
        # Insertion-ordered set of flight ids whose live doc cannot be used.
        self._unreadable_flights: dict[str, None] = {}
        self._last_connect_attempt = 0.0
        self._last_ping_ok = 0.0
        self._reported_down = False
        self._raw_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._raw_stop = threading.Event()
        self._raw_thread: threading.Thread | None = None
        self._raw_pubsub: Any = None
        self._raw_callback: Callable[[dict[str, Any]], None] | None = None
        self._pending_pops: set[str] = set()
        # alert_id -> (flight_id, doc): active alert refreshes queued for the
        # tick's write_live_planes pipeline (guarded by _mem_lock).
        self._dirty_alerts: dict[str, tuple[str, dict[str, Any]]] = {}
        # Extra fields the engine publishes in its heartbeat (history health).
        self._engine_health: dict[str, Any] = {}
        self._engine_token = uuid.uuid4().hex
        if memory_only:
            self._reported_down = True
            log.info("Live store running in memory-only mode (no Redis).")
        else:
            self._connect()

    def _connect(self) -> None:
        """Create the Redis client, verify it, and publish it as ``self.client``.

        Safe to call repeatedly: on failure the store keeps running with the
        in-memory buffer and retries later via :meth:`_redis`. The network
        round trips (up to the 2 s socket timeouts each) run without
        ``_conn_lock``, and the client is published only once usable: for the
        engine, after it re-claimed the heartbeat and backfilled Redis, so no
        other thread writes through it before ownership is settled.
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
            if reconnected and self.writer:
                # A RedisError here is an outage, not a rival engine: the next
                # reconnect claims again.
                if self._claim(client, time.time()):
                    self._backfill_redis_from_mem(client)
                else:
                    self.writer = False
                    log.error(
                        "Reconnected to Redis but another engine holds "
                        "live:engine; yielding the live writer"
                    )
        except RedisError as exc:
            _close_quietly(client)
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
            # Reconnect backfill rewrites flights; forget what was sent.
            self._written_docs.clear()
        _close_quietly(current)
        self._report_down(exc)

    def _redis(self) -> redis.Redis | None:
        """Return a connected client snapshot, or None to use the memory buffer.

        Reconnects automatically (throttled) so a Redis that comes up after
        startup, or recovers from an outage, is picked up without a restart.
        Only one thread reconnects at a time; the others get None at once
        instead of queueing behind its socket timeouts (portal readers would
        stall ~2 s per attempt while Redis is down).
        """
        if self.memory_only:
            return None
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
        payload = _engine_heartbeat_payload(
            self._engine_token, now, self._engine_health
        )
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
        client = self._redis()
        if client is None:
            return True
        try:
            claimed = self._claim(client, time.time())
        except RedisError as exc:
            # Redis went away mid-tick. That is not evidence of another
            # engine: keep tracking in memory and re-claim on reconnect.
            log.warning("Could not refresh engine heartbeat: %s", exc)
            self._mark_disconnected(exc, client)
            return True
        if not claimed:
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
        try:
            return self._claim(client, now)
        except RedisError as exc:
            log.error("Failed to claim engine heartbeat: %s", exc)
            return False

    def _claim(self, client: redis.Redis, now: float) -> bool:
        """Claim the heartbeat; False only for a fresh foreign token.

        Raises :class:`RedisError` so callers can tell an outage from a rival.
        """
        payload = _engine_heartbeat_payload(
            self._engine_token, now, self._engine_health
        )
        if client.set(_KEY_ENGINE, payload, ex=LIVE_ENGINE_TTL_SECONDS, nx=True):
            return True
        raw = client.get(_KEY_ENGINE)
        doc = _safe_json_loads(raw) if raw else None
        if not self._owns_heartbeat(doc) and self._heartbeat_is_fresh(doc, now):
            return False
        client.set(_KEY_ENGINE, payload, ex=LIVE_ENGINE_TTL_SECONDS)
        return True

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
        with self._mem_lock:
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

    def set_engine_health(self, **fields: Any) -> None:
        """Fields added to the ``live:engine`` heartbeat value from now on."""
        self._engine_health = dict(fields)

    def write_live_planes(self, planes: dict[str, Plane]) -> None:
        if not planes and not self._dirty_alerts:
            return
        if not self.writer and not self.memory_only:
            return
        # One round trip per tick for every flight, telemetry, and active
        # alert refresh.
        client = self._redis() if self.writer else None
        pipe = client.pipeline(transaction=False) if client is not None else None
        queued = False
        for plane in planes.values():
            queued = self._upsert_live_flight(plane, pipe) or queued
        queued = self._flush_dirty_alerts(pipe) or queued
        if pipe is None or not queued:
            return
        try:
            pipe.execute()
        except RedisError as exc:
            log.error("Failed to write %d live flight(s): %s", len(planes), exc)
            self._mark_disconnected(exc, client)

    def record_alert_episode(
        self,
        plane: Plane,
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
        flight_id = plane.flight_id
        doc = self._alert_doc(
            plane,
            meta,
            payload,
            alert_id=alert_id,
            activated_at=activated_at,
            active=active,
            deactivated_at=deactivated_at,
        )
        with self._mem_lock:
            # A queued refresh must not re-activate it after this write.
            self._dirty_alerts.pop(alert_id, None)
            if active:
                # Re-recording an ongoing episode (segment handover) must not
                # roll last_updated back to activated_at: a crash right after
                # would archive it as a zero-length episode.
                previous = self._mem.active_alerts.get(alert_id) or {}
                last_seen = previous.get("last_updated")
                if isinstance(last_seen, (int, float)) and last_seen > (
                    doc.get("last_updated") or 0
                ):
                    doc["last_updated"] = last_seen
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
            pipe.expire(alerts_key, LIVE_FLIGHT_TTL_SECONDS)
            pipe.hset(_KEY_ALERT_EPISODES, alert_id, encoded)
            pipe.execute()
        except RedisError as exc:
            log.error("Failed to record alert episode for %s: %s", flight_id, exc)
            # Without this the reconnect backfill never runs and a failed
            # deactivation leaves a ghost active alert in Redis.
            self._mark_disconnected(exc, client)

    def update_active_alert(
        self,
        plane: Plane,
        alert_id: str,
        meta: dict[str, Any],
        payload: dict[str, Any],
        timestamp: float,
    ) -> None:
        """Refresh ETA, position, and telemetry on an active alert.

        Memory holds the episode (including ``activated_at``); the Redis write
        is queued for the tick's :meth:`write_live_planes` pipeline instead of
        costing a read and a round trip per alert.
        """
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
        flight_id = plane.flight_id
        with self._mem_lock:
            self._mem.active_alerts[alert_id] = doc
            flight_alerts = self._mem.alerts.get(flight_id, [])
            for i, item in enumerate(flight_alerts):
                if item.get("alert_id") == alert_id:
                    flight_alerts[i] = doc
                    break

            for i, item in enumerate(self._mem.alert_episodes):
                if item.get("alert_id") == alert_id:
                    self._mem.alert_episodes[i] = doc
                    break
            if self.writer and not self.memory_only:
                self._dirty_alerts[alert_id] = (flight_id, doc)

    def _flush_dirty_alerts(self, pipe: Any) -> bool:
        """Queue the pending active alert refreshes on *pipe*. Returns queued.

        Without a pipe (Redis down) they are dropped: memory has them, and the
        reconnect backfill rewrites every active alert from memory.
        """
        with self._mem_lock:
            if not self._dirty_alerts:
                return False
            dirty, self._dirty_alerts = self._dirty_alerts, {}
        if pipe is None:
            return False
        for alert_id, (flight_id, doc) in dirty.items():
            encoded = json_dumps(doc)
            alerts_key = _KEY_ALERTS.format(flight_id=flight_id)
            pipe.hset(_KEY_ACTIVE_ALERTS, alert_id, encoded)
            pipe.hset(alerts_key, alert_id, encoded)
            pipe.expire(alerts_key, LIVE_FLIGHT_TTL_SECONDS)
            pipe.hset(_KEY_ALERT_EPISODES, alert_id, encoded)
        return True

    def carry_active_alerts(self, old_flight_id: str, new_flight_id: str) -> int:
        """Continue active episodes on the flight that continues *old_flight_id*.

        Used when a long track is split into segments: the episode keeps its
        alert_id and ``activated_at`` (no deactivate/activate notification).
        The old flight keeps its copy until that segment is archived, so the
        segment where the aircraft entered the zone gets the alert row (and
        the retain evidence); the final segment's write updates the row.
        :meth:`pop_flight` of the old segment leaves the carried episode in
        the shared indexes. Returns how many were carried.
        """
        with self._mem_lock:
            moved: dict[str, dict[str, Any]] = {}
            for alert_id, doc in list(self._mem.active_alerts.items()):
                if doc.get("flight_id") == old_flight_id:
                    moved[alert_id] = {**doc, "flight_id": new_flight_id}
                    self._mem.active_alerts[alert_id] = moved[alert_id]
            if not moved:
                return 0
            old_list = self._mem.alerts[old_flight_id]
            listed = {a.get("alert_id") for a in old_list}
            old_list.extend(
                {**doc, "flight_id": old_flight_id}
                for alert_id, doc in moved.items()
                if alert_id not in listed
            )
            self._mem.alerts[new_flight_id].extend(moved.values())
            self._mem.alert_episodes = [
                moved.get(cast(str, a.get("alert_id")), a)
                for a in self._mem.alert_episodes
            ]
            for alert_id, (flight_id, doc) in list(self._dirty_alerts.items()):
                if flight_id == old_flight_id:
                    self._dirty_alerts[alert_id] = (
                        new_flight_id,
                        {**doc, "flight_id": new_flight_id},
                    )
        client = self._redis() if self.writer else None
        if client is None:
            return len(moved)  # the reconnect backfill writes them from memory
        try:
            pipe = client.pipeline()
            new_key = _KEY_ALERTS.format(flight_id=new_flight_id)
            for alert_id, doc in moved.items():
                encoded = json_dumps(doc)
                pipe.hset(_KEY_ACTIVE_ALERTS, alert_id, encoded)
                pipe.hset(_KEY_ALERT_EPISODES, alert_id, encoded)
                pipe.hset(new_key, alert_id, encoded)
            pipe.expire(new_key, LIVE_FLIGHT_TTL_SECONDS)
            pipe.execute()
        except RedisError as exc:
            log.error("Failed to carry alerts of %s: %s", old_flight_id, exc)
            self._mark_disconnected(exc, client)
        return len(moved)

    def _alert_doc(
        self,
        plane: Plane,
        meta: dict[str, Any],
        payload: dict[str, Any],
        *,
        alert_id: str,
        activated_at: float,
        active: bool,
        deactivated_at: float | None = None,
    ) -> dict[str, Any]:
        flight_id = plane.flight_id
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
                    flight = _safe_json_loads(raw) if raw else None
                    if not isinstance(flight, dict):
                        continue
                    last_tel = _safe_json_loads(last_raw[0]) if last_raw else None
                    results.append(
                        live_flight_summary(
                            flight, last_tel if isinstance(last_tel, dict) else None
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
            return self._read_telemetry(
                client, flight_id, since, limit=limit, newest=newest
            )
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
            raw = client.zrevrange(_KEY_TELEMETRY.format(flight_id=flight_id), 0, 0)
        except RedisError as exc:
            log.error("Failed to read latest telemetry for %s: %s", flight_id, exc)
            self._mark_disconnected(exc, client)
            self._raise_if_reader_disconnected()
            return None
        parsed = _safe_json_loads(raw[0]) if raw else None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _read_telemetry(
        client: redis.Redis,
        flight_id: str,
        since: float,
        *,
        limit: int | None = None,
        newest: bool = False,
    ) -> list[dict[str, Any]]:
        key = _KEY_TELEMETRY.format(flight_id=flight_id)
        low = f"({since}" if since > 0 else "-inf"
        if limit is None:
            if since > 0:
                raw_points = client.zrangebyscore(key, low, "+inf")
            else:
                raw_points = client.zrange(key, 0, -1)
        elif not newest:
            raw_points = client.zrangebyscore(key, low, "+inf", start=0, num=limit)
        elif since > 0:
            raw_points = client.zrevrangebyscore(key, "+inf", low, start=0, num=limit)
            raw_points.reverse()
        else:
            raw_points = client.zrange(key, -limit, -1)
        points = []
        for point in raw_points:
            parsed = _safe_json_loads(point)
            if isinstance(parsed, dict):
                points.append(parsed)
        return points

    @staticmethod
    def _live_point(flight_id: str, point: dict[str, Any]) -> dict[str, Any]:
        live = {
            "flight_id": flight_id,
            "icao": point.get("icao"),
            "timestamp": point.get("timestamp"),
            "latitude": point.get("latitude"),
            "longitude": point.get("longitude"),
            "altitude": point.get("altitude"),
            "speed": point.get("speed"),
            "heading": point.get("heading"),
        }
        # Optional fields, copied only when present: GNSS height (TC 20-22
        # positions) and the provenance WEBSOCKET.md promises on the push.
        for key in _LIVE_POINT_OPTIONAL:
            if point.get(key) is not None:
                live[key] = point[key]
        return live

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
                for flight_id in cast("set[str]", client.smembers(_KEY_FLIGHTS)):
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
        """Alert episodes from Redis merged with memory by alert_id.

        Memory wins: it holds this process's latest writes. Redis also holds
        what a previous engine process wrote (a restart closes a leftover
        flight's active episode in memory, while its earlier closed episodes
        exist only in Redis). The engine raises :class:`LiveUnavailable`
        rather than answer from memory alone for a flight it does not own, so
        that flight is archived later instead of without its alerts.
        """

        def by_id(docs: list[dict[str, Any]]) -> dict[Any, dict[str, Any]]:
            return {doc.get("alert_id") or id(doc): doc for doc in docs}

        # Memory's latest state of every episode it holds (including closed
        # ones, and an old segment's copy of a carried episode).
        mem_all = by_id(self._mem_alerts(flight_id, active_only=False))
        # What memory contributes: for active_only only its active set, so a
        # carried episode's old-segment copy is never reported as active there.
        mem_docs = (
            by_id(self._mem_alerts(flight_id, active_only=True))
            if active_only
            else mem_all
        )
        client = self._redis()
        if client is None:
            if self._writer_needs_redis_for(flight_id):
                raise LiveUnavailable(
                    f"live store is unavailable; cannot read alerts of {flight_id}"
                )
            self._raise_if_reader_disconnected()
            alerts = list(mem_docs.values())
        else:
            try:
                redis_docs = self._redis_alerts(client, flight_id, active_only)
            except RedisError as exc:
                log.error("Failed to read live alerts: %s", exc)
                self._mark_disconnected(exc, client)
                if self.writer and not self.memory_only:
                    raise LiveUnavailable(f"could not read live alerts: {exc}") from exc
                self._raise_if_reader_disconnected()
                return []
            merged = by_id(redis_docs)
            merged.update(mem_docs)
            # Memory wins for episodes it knows, e.g. a deactivation whose
            # Redis write failed must not resurrect as active.
            alerts = [mem_all.get(key, doc) for key, doc in merged.items()]
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

    def _writer_needs_redis_for(self, flight_id: str | None) -> bool:
        """Memory is complete only for flights this engine process wrote."""
        return (
            self.writer
            and not self.memory_only
            and bool(flight_id)
            and flight_id not in self._mem.flights
        )

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
        with self._mem_lock:
            mem_flight = self._mem.flights.pop(flight_id, None)
            mem_alerts = self._mem.alerts.pop(flight_id, [])
            self._mem.telemetry.pop(flight_id, None)
            self._last_telemetry_ts.pop(flight_id, None)
            self._written_docs.pop(flight_id, None)
            self._mem.alert_episodes = [
                a for a in self._mem.alert_episodes if a.get("flight_id") != flight_id
            ]
            for a in list(self._mem.active_alerts.values()):
                if a.get("flight_id") == flight_id:
                    self._mem.active_alerts.pop(a["alert_id"], None)
            for alert_id, (dirty_flight, _doc) in list(self._dirty_alerts.items()):
                if dirty_flight == flight_id:
                    del self._dirty_alerts[alert_id]

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

    def stray_active_alerts(self) -> list[dict[str, Any]]:
        """Active alert docs whose flight is not in ``live:flights``.

        An alert written for a brand-new flight just before a crash (ahead of
        that tick's flight write) is invisible to the startup archive and the
        orphan sweep, so it would stay active forever. Entries that cannot be
        parsed into a flight cannot be closed properly and are dropped.
        """
        if self.memory_only:
            return []
        client = self._redis()
        if client is None:
            return []
        try:
            live = set(client.smembers(_KEY_FLIGHTS))
            active = cast("dict[str, str]", client.hgetall(_KEY_ACTIVE_ALERTS))
            strays: list[dict[str, Any]] = []
            broken: list[str] = []
            for alert_id, raw in active.items():
                doc = _safe_json_loads(raw)
                flight_id = doc.get("flight_id") if isinstance(doc, dict) else None
                if not isinstance(flight_id, str) or "-" not in flight_id:
                    broken.append(alert_id)
                elif flight_id not in live:
                    strays.append({**doc, "alert_id": alert_id})
            if broken and self.writer:
                log.warning("Dropping %d unreadable active alert(s)", len(broken))
                client.hdel(_KEY_ACTIVE_ALERTS, *broken)
        except RedisError as exc:
            log.error("Failed to read active alerts: %s", exc)
            self._mark_disconnected(exc, client)
            return []
        return strays

    def flight_ids(self) -> set[str]:
        client = self._redis()
        if client is not None:
            try:
                return set(cast("set[str]", client.smembers(_KEY_FLIGHTS)))
            except RedisError as exc:
                self._mark_disconnected(exc, client)
        return set(self._mem.flights)

    def planes_for_finalize(self) -> list[Plane]:
        planes = []
        for flight_id in self.flight_ids():
            plane = self.plane_for_finalize(flight_id)
            if plane is not None:
                planes.append(plane)
        return planes

    def _skip_unreadable(
        self, client: redis.Redis | None, flight_id: str, reason: str
    ) -> None:
        """Remember (bounded) a live doc that cannot be archived; log it once.

        It is kept, not deleted (it may come from another PyAerial version),
        but given the live TTL if it has none so Redis lets go of it.
        """
        if flight_id in self._unreadable_flights:
            return
        while len(self._unreadable_flights) >= _UNREADABLE_FLIGHTS_MAX:
            del self._unreadable_flights[next(iter(self._unreadable_flights))]
        self._unreadable_flights[flight_id] = None
        log.warning(
            "Live copy of %s is unreadable (%s); not archiving it", flight_id, reason
        )
        if client is None or not self.writer:
            return
        key = _KEY_FLIGHT.format(flight_id=flight_id)
        try:
            if client.ttl(key) == -1:  # exists without an expiry
                client.expire(key, LIVE_FLIGHT_TTL_SECONDS)
        except RedisError as exc:
            self._mark_disconnected(exc, client)

    def plane_for_finalize(self, flight_id: str) -> Plane | None:
        if flight_id in self._unreadable_flights:
            return None
        doc: dict[str, Any] | None = self._mem.flights.get(flight_id)
        client = self._redis() if doc is None else None
        if client is not None:
            try:
                raw = client.get(_KEY_FLIGHT.format(flight_id=flight_id))
                if raw is None and self.writer:
                    # Its keys hit LIVE_FLIGHT_TTL_SECONDS: nothing is left to
                    # archive. Drop what still names it, or every orphan sweep
                    # re-reads it (and a ghost active alert stays forever).
                    self._delete_redis_flight(client, flight_id, all_episodes=True)
                    log.warning(
                        "Live copy of %s expired before it was archived", flight_id
                    )
            except RedisError as exc:
                # An outage is not an expiry: keep everything for a retry.
                self._mark_disconnected(exc, client)
                raw = None
            parsed = _safe_json_loads(raw) if raw else None
            doc = parsed if isinstance(parsed, dict) else None
            if raw and doc is None:
                self._skip_unreadable(client, flight_id, "not a JSON object")
        if not doc:
            return None
        icao = live_doc_icao(doc)
        if not icao:
            self._skip_unreadable(client, flight_id, "no ICAO address")
            return None
        return plane_from_live(doc, icao, self.get_telemetry(flight_id))

    @staticmethod
    def _delete_redis_flight(
        client: redis.Redis, flight_id: str, *, all_episodes: bool = False
    ) -> dict[str, Any]:
        """Delete a flight's keys and index entries; returns what was stored.

        ``all_episodes`` also scans the shared episode index for the flight,
        for when its per-flight alerts hash (the usual source) has expired.
        """
        raw_flight = client.get(_KEY_FLIGHT.format(flight_id=flight_id))
        # decode_responses=True: replies are str.
        flight_alerts_raw = cast(
            "dict[str, str]", client.hgetall(_KEY_ALERTS.format(flight_id=flight_id))
        )
        raw_alerts = list(flight_alerts_raw.values())
        active_raw = []
        active = cast("dict[str, str]", client.hgetall(_KEY_ACTIVE_ALERTS))
        for alert_id, raw in active.items():
            parsed = _safe_json_loads(raw)
            if isinstance(parsed, dict) and parsed.get("flight_id") == flight_id:
                active_raw.append(alert_id)
        # Only index entries still filed under this flight: an episode carried
        # on to the next segment keeps a copy here until this one is archived.
        episode_ids: set[str] = set()
        if flight_alerts_raw:
            ids = list(flight_alerts_raw)
            stored = cast("list[str | None]", client.hmget(_KEY_ALERT_EPISODES, ids))
            for alert_id, episode in zip(ids, stored, strict=True):
                parsed = _safe_json_loads(episode) if episode else None
                if not isinstance(parsed, dict) or parsed.get("flight_id") == flight_id:
                    episode_ids.add(alert_id)
        if all_episodes:
            episodes = cast("dict[str, str]", client.hgetall(_KEY_ALERT_EPISODES))
            for alert_id, raw in episodes.items():
                parsed = _safe_json_loads(raw)
                if isinstance(parsed, dict) and parsed.get("flight_id") == flight_id:
                    episode_ids.add(alert_id)
        pipe = client.pipeline()
        pipe.srem(_KEY_FLIGHTS, flight_id)
        pipe.delete(
            _KEY_FLIGHT.format(flight_id=flight_id),
            _KEY_TELEMETRY.format(flight_id=flight_id),
            _KEY_ALERTS.format(flight_id=flight_id),
        )
        for alert_id in active_raw:
            pipe.hdel(_KEY_ACTIVE_ALERTS, alert_id)
        if episode_ids:
            pipe.hdel(_KEY_ALERT_EPISODES, *episode_ids)
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

        The web app subscribes to ``live:raw`` once and fans batches out as
        JSON on ``/ws/raw`` and dump1090 Beast binary on ``/ws/beast`` plus
        the optional Beast TCP port. Empty batches are ignored.
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

    def _upsert_live_flight(self, plane: Plane, pipe: Any = None) -> bool:
        """Update one live flight; queue Redis writes on *pipe*.

        Returns whether anything was queued. Without *pipe* (and with a
        writable Redis), the writes are executed immediately.
        """
        flight_doc = live_flight_doc(plane)
        flight_id = flight_doc["flight_id"]
        icao = flight_doc["icao"]
        with self._mem_lock:
            self._mem.flights[flight_id] = flight_doc

        client = None
        own_pipe = pipe is None
        if own_pipe and self.writer:
            client = self._redis()
            pipe = client.pipeline() if client is not None else None
        queued = self._write_telemetry_points(plane, flight_id, icao, pipe)

        if self.writer and pipe is not None:
            encoded = json_dumps(flight_doc)
            if self._written_docs.get(flight_id) != encoded:
                pipe.sadd(_KEY_FLIGHTS, flight_id)
                # end_time changes with every packet, so a tracked flight
                # re-arms its TTL long before it could lapse.
                pipe.set(
                    _KEY_FLIGHT.format(flight_id=flight_id),
                    encoded,
                    ex=LIVE_FLIGHT_TTL_SECONDS,
                )
                self._written_docs[flight_id] = encoded
                queued = True
        if own_pipe and pipe is not None and queued:
            try:
                pipe.execute()
            except RedisError as exc:
                log.error("Failed to upsert live flight %s: %s", flight_id, exc)
                self._mark_disconnected(exc, client)
        return queued

    def _write_telemetry_points(
        self, plane: Plane, flight_id: str, icao: str, pipe: Any = None
    ) -> bool:
        """Append new samples to memory and queue them on *pipe*. Returns queued."""
        last_written = self._last_telemetry_ts.get(flight_id, 0.0)
        samples = list(iter_telemetry_samples(plane, since=last_written))
        if not samples:
            return False

        key = _KEY_TELEMETRY.format(flight_id=flight_id) if pipe is not None else None

        new_points: list[dict[str, Any]] = []
        for sample in samples:
            timestamp = sample[0]
            point = live_telemetry_point(plane, sample, icao)
            new_points.append(point)
            if pipe is not None and key:
                pipe.zadd(key, {json_dumps(point): timestamp})
            last_written = max(last_written, timestamp)
        if pipe is not None and key:
            pipe.expire(key, LIVE_FLIGHT_TTL_SECONDS)

        keep = self.telemetry_keep_seconds
        cutoff = time.time() - keep
        with self._mem_lock:
            self._mem.telemetry[flight_id].extend(new_points)
            # Advance the cursor after the in-memory write so a failed Redis
            # execute does not duplicate mem points. Redis holes are repaired
            # by ``_backfill_redis_from_mem`` on reconnect.
            self._last_telemetry_ts[flight_id] = last_written
            if keep > 0:
                points = self._mem.telemetry[flight_id]
                trimmed = [point for point in points if point["timestamp"] >= cutoff]
                self._mem.telemetry[flight_id] = trimmed or points[-1:]
            kept = self._mem.telemetry[flight_id]
            last_point = kept[-1] if kept else None
        if keep > 0 and pipe is not None and key:
            pipe.zremrangebyscore(key, "-inf", cutoff)
            last_ts = last_point.get("timestamp") if last_point else None
            if isinstance(last_ts, (int, float)) and last_ts < cutoff:
                pipe.zadd(key, {json_dumps(last_point): last_ts})
        return pipe is not None

    def _backfill_redis_from_mem(self, client: redis.Redis | None = None) -> None:
        """Replay in-memory flights/telemetry/alerts after a Redis reconnect.

        *client* is the not yet published reconnect client. Raises
        :class:`RedisError`; the caller then does not publish it.
        """
        client = client if client is not None else self.client
        if not self.writer or client is None:
            return
        # Copy under the lock, then do the network I/O without it: the engine
        # thread keeps writing to memory meanwhile (iterating the live dicts
        # raised "changed size during iteration" and aborted the reconnect).
        with self._mem_lock:
            flights = dict(self._mem.flights)
            telemetry = {
                flight_id: list(self._mem.telemetry.get(flight_id, ()))
                for flight_id in flights
            }
            active_alerts = dict(self._mem.active_alerts)
            episodes = list(self._mem.alert_episodes)
            pending_pops = set(self._pending_pops)
        try:
            # Only delete keys this process already decided to pop. Redis ids
            # that are not in mem may be leftovers from a previous engine and
            # must be archived by the engine, not wiped here.
            for flight_id in pending_pops:
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
            owned = set(flights)
            ghosts = []
            stored = cast("dict[str, str]", client.hgetall(_KEY_ACTIVE_ALERTS))
            for alert_id, raw in stored.items():
                if alert_id in active_alerts:
                    continue
                parsed = _safe_json_loads(raw)
                if isinstance(parsed, dict) and parsed.get("flight_id") in owned:
                    ghosts.append(alert_id)
            pipe = client.pipeline()
            if ghosts:
                pipe.hdel(_KEY_ACTIVE_ALERTS, *ghosts)
            for flight_id, doc in flights.items():
                pipe.sadd(_KEY_FLIGHTS, flight_id)
                pipe.set(
                    _KEY_FLIGHT.format(flight_id=flight_id),
                    json_dumps(doc),
                    ex=LIVE_FLIGHT_TTL_SECONDS,
                )
                key = _KEY_TELEMETRY.format(flight_id=flight_id)
                for point in telemetry[flight_id]:
                    ts = point.get("timestamp")
                    if ts is None:
                        continue
                    pipe.zadd(key, {json_dumps(point): ts})
                pipe.expire(key, LIVE_FLIGHT_TTL_SECONDS)
            for alert_id, doc in active_alerts.items():
                encoded = json_dumps(doc)
                pipe.hset(_KEY_ACTIVE_ALERTS, alert_id, encoded)
            for doc in episodes:
                episode_id = doc.get("alert_id")
                episode_flight = doc.get("flight_id")
                if not episode_id:
                    continue
                encoded = json_dumps(doc)
                pipe.hset(_KEY_ALERT_EPISODES, episode_id, encoded)
                if episode_flight:
                    alerts_key = _KEY_ALERTS.format(flight_id=episode_flight)
                    pipe.hset(alerts_key, episode_id, encoded)
                    pipe.expire(alerts_key, LIVE_FLIGHT_TTL_SECONDS)
            pipe.execute()
            # The heartbeat was just written by the claim that preceded this.
        except RedisError as exc:
            log.error("Failed to backfill Redis from memory: %s", exc)
            raise
