"""The engine's side of the Redis live store.

:class:`LiveWriter` claims and refreshes the ``live:engine`` heartbeat,
writes live flights and telemetry (one pipeline per tick), records alert
episodes (:mod:`.episodes`), pops archived flights and rebuilds leftovers
for the archive (:mod:`.recovery`), and publishes raw frames. Everything it
writes is mirrored in memory, so tracking continues while Redis is down and
the mirror is replayed into Redis on reconnect. With ``memory_only`` (the
engine's isolated mode) the mirror is the whole store.
"""

from __future__ import annotations

import logging
import time
from typing import Any, cast

import redis
from redis.exceptions import RedisError

from pyaerial.jsonutil import dumps as json_dumps
from pyaerial.models import Plane, iter_telemetry_samples
from pyaerial.store.codec import live_flight_doc, live_telemetry_point
from pyaerial.store.live.episodes import EpisodesMixin
from pyaerial.store.live.keys import (
    ENGINE_TTL_SECONDS,
    KEY_ACTIVE_ALERTS,
    KEY_ALERT_EPISODES,
    KEY_ALERTS,
    KEY_ENGINE,
    KEY_FLIGHT,
    KEY_FLIGHTS,
    KEY_TELEMETRY,
    LIVE_FLIGHT_TTL_SECONDS,
    RAW_CHANNEL,
    engine_heartbeat_payload,
    heartbeat_is_fresh,
    heartbeat_owned_by,
    safe_json_loads,
)
from pyaerial.store.live.recovery import RecoveryMixin

log = logging.getLogger("pyaerial.store.redis")


class LiveWriter(EpisodesMixin, RecoveryMixin):
    """Live store for the tracking engine (the only writer of the live keys).

    ``memory_only`` keeps everything in memory (isolated mode); the store
    then also serves reads, so it can back a portal in-process.
    ``writer=False`` opens a maintenance handle (``pyaerial reset``): it
    never claims ``live:engine`` or writes live keys, and its reads raise
    :class:`LiveUnavailable` while Redis is down, but it can delete flights.
    """

    def __init__(
        self,
        redis_uri: str,
        *,
        memory_only: bool = False,
        telemetry_keep_seconds: float = 600.0,
        writer: bool = True,
    ):
        super().__init__(
            redis_uri,
            memory_only=memory_only,
            telemetry_keep_seconds=telemetry_keep_seconds,
            writer=writer,
        )
        if memory_only:
            self._reported_down = True
            log.info("Live store running in memory-only mode (no Redis).")
        else:
            self._connect()

    def _on_reconnect(self, client: redis.Redis) -> None:
        if not self.writer:
            return
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

    # -- heartbeat and writer claim -------------------------------------------

    def _owns_heartbeat(self, doc: Any) -> bool:
        return heartbeat_owned_by(doc, self._engine_token)

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
        payload = engine_heartbeat_payload(self._engine_token, now, self._engine_health)
        try:
            raw = client.get(KEY_ENGINE)
            doc = safe_json_loads(raw) if raw else None
            if raw and not self._owns_heartbeat(doc) and heartbeat_is_fresh(doc, now):
                return
            client.set(KEY_ENGINE, payload, ex=ENGINE_TTL_SECONDS)
        except RedisError as exc:
            log.error("Failed to write engine heartbeat: %s", exc)
            self._mark_disconnected(exc, client)

    def other_engine_is_live(self) -> bool:
        """True when another process holds a fresh live:engine heartbeat."""
        if self.memory_only:
            return False
        client = self._redis()
        if client is None:
            return False
        try:
            raw = client.get(KEY_ENGINE)
        except RedisError as exc:
            log.debug("Could not read engine heartbeat: %s", exc)
            return False
        doc = safe_json_loads(raw) if raw else None
        if self._owns_heartbeat(doc):
            return False
        return heartbeat_is_fresh(doc)

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
        payload = engine_heartbeat_payload(self._engine_token, now, self._engine_health)
        if client.set(KEY_ENGINE, payload, ex=ENGINE_TTL_SECONDS, nx=True):
            return True
        raw = client.get(KEY_ENGINE)
        doc = safe_json_loads(raw) if raw else None
        if not self._owns_heartbeat(doc) and heartbeat_is_fresh(doc, now):
            return False
        client.set(KEY_ENGINE, payload, ex=ENGINE_TTL_SECONDS)
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
            raw = client.get(KEY_ENGINE)
            doc = safe_json_loads(raw) if raw else None
            if raw and not self._owns_heartbeat(doc):
                return
            client.delete(KEY_ENGINE)
        except RedisError as exc:
            log.debug("Could not clear engine heartbeat: %s", exc)

    def set_engine_health(self, **fields: Any) -> None:
        """Fields added to the ``live:engine`` heartbeat value from now on."""
        self._engine_health = dict(fields)

    # -- flights and telemetry ------------------------------------------------

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
                pipe.sadd(KEY_FLIGHTS, flight_id)
                # end_time changes with every packet, so a tracked flight
                # re-arms its TTL long before it could lapse.
                pipe.set(
                    KEY_FLIGHT.format(flight_id=flight_id),
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

        key = KEY_TELEMETRY.format(flight_id=flight_id) if pipe is not None else None

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

    # -- reconnect backfill ---------------------------------------------------

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
            stored = cast("dict[str, str]", client.hgetall(KEY_ACTIVE_ALERTS))
            for alert_id, raw in stored.items():
                if alert_id in active_alerts:
                    continue
                parsed = safe_json_loads(raw)
                if isinstance(parsed, dict) and parsed.get("flight_id") in owned:
                    ghosts.append(alert_id)
            pipe = client.pipeline()
            if ghosts:
                pipe.hdel(KEY_ACTIVE_ALERTS, *ghosts)
            for flight_id, doc in flights.items():
                pipe.sadd(KEY_FLIGHTS, flight_id)
                pipe.set(
                    KEY_FLIGHT.format(flight_id=flight_id),
                    json_dumps(doc),
                    ex=LIVE_FLIGHT_TTL_SECONDS,
                )
                key = KEY_TELEMETRY.format(flight_id=flight_id)
                for point in telemetry[flight_id]:
                    ts = point.get("timestamp")
                    if ts is None:
                        continue
                    pipe.zadd(key, {json_dumps(point): ts})
                pipe.expire(key, LIVE_FLIGHT_TTL_SECONDS)
            for alert_id, doc in active_alerts.items():
                encoded = json_dumps(doc)
                pipe.hset(KEY_ACTIVE_ALERTS, alert_id, encoded)
            for doc in episodes:
                episode_id = doc.get("alert_id")
                episode_flight = doc.get("flight_id")
                if not episode_id:
                    continue
                encoded = json_dumps(doc)
                pipe.hset(KEY_ALERT_EPISODES, episode_id, encoded)
                if episode_flight:
                    alerts_key = KEY_ALERTS.format(flight_id=episode_flight)
                    pipe.hset(alerts_key, episode_id, encoded)
                    pipe.expire(alerts_key, LIVE_FLIGHT_TTL_SECONDS)
            pipe.execute()
            # The heartbeat was just written by the claim that preceded this.
        except RedisError as exc:
            log.error("Failed to backfill Redis from memory: %s", exc)
            raise

    # -- raw frames -----------------------------------------------------------

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
            client.publish(RAW_CHANNEL, json_dumps(payload))
        except RedisError as exc:
            log.debug("Failed to publish raw frames: %s", exc)
