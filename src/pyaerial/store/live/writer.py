"""The engine's side of the Redis live store: a cache of what it tracks.

:class:`LiveWriter` claims and refreshes the ``live:engine`` heartbeat,
publishes the tracked flights, their telemetry rings and alert episodes
(one pipeline per tick), keeps the episode outbox while the archive is down,
and publishes raw frames. Redis holds nothing the archive needs: after a
failed write or a reconnect the next tick republishes everything, and keys
of flights no longer tracked are deleted by the publish that drops them.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, cast

import redis
from redis.exceptions import RedisError

from pyaerial.jsonutil import dumps as json_dumps
from pyaerial.models import Plane, iter_telemetry_samples
from pyaerial.store.codec import live_flight_doc, live_telemetry_point
from pyaerial.store.live._queries import LiveQueries
from pyaerial.store.live.keys import (
    ENGINE_TTL_SECONDS,
    KEY_ACTIVE_ALERTS,
    KEY_ALERT_EPISODES,
    KEY_ALERTS,
    KEY_ENGINE,
    KEY_EPISODE_OUTBOX,
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

log = logging.getLogger("pyaerial.store.redis")


class LiveWriter(LiveQueries):
    """Live store for the tracking engine (the only writer of the live keys).

    ``writer=False`` opens a maintenance handle (``pyaerial reset``): it
    never claims ``live:engine`` or publishes, but it reads and can delete
    flights. Reads come from Redis (:class:`LiveQueries`).
    """

    def __init__(
        self,
        redis_uri: str,
        *,
        telemetry_keep_seconds: float = 600.0,
        writer: bool = True,
    ):
        super().__init__(redis_uri)
        self.telemetry_keep_seconds = telemetry_keep_seconds
        # Whether this process owns the Redis live keys (claims the heartbeat
        # on reconnect, publishes). Cleared on yielding to another engine.
        self.writer = writer
        self._engine_token = uuid.uuid4().hex
        # Extra fields the engine publishes in its heartbeat (history health).
        self._engine_health: dict[str, Any] = {}
        # What this process has put in Redis. Only a successful publish moves
        # it forward; a failure forces a full republish (``_resync``).
        self._published: set[str] = set()
        self._written_docs: dict[str, str] = {}
        self._telemetry_through: dict[str, float] = {}
        # flight_id -> alert_id -> latest episode doc, for tracked flights;
        # dirty alert ids are written by the next publish.
        self._episodes: dict[str, dict[str, dict[str, Any]]] = {}
        self._dirty_episodes: set[str] = set()
        self._resync = True
        self._connect()

    def _on_reconnect(self, client: redis.Redis) -> None:
        if not self.writer:
            return
        # A RedisError here is an outage, not a rival engine: the next
        # reconnect claims again.
        if self._claim(client, time.time()):
            self._resync = True
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
        client = self._redis() if self.writer else None
        if client is None:
            return
        now = time.time()
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

        Redis down: keep tracking (the archive records it; Redis is only the
        portal's cache). Once Redis is back, reclaim the heartbeat or yield
        if another engine holds a fresh token.
        """
        if not self.writer:
            return False
        client = self._redis()
        if client is None:
            return True
        try:
            claimed = self._claim(client, time.time())
        except RedisError as exc:
            # Redis went away mid-tick. That is not evidence of another
            # engine: keep tracking and re-claim on reconnect.
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
        client = self._redis()
        if client is None:
            return False
        try:
            return self._claim(client, time.time())
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

    # -- the tick's publish ---------------------------------------------------

    def cache_episode(self, doc: Mapping[str, Any]) -> None:
        """Keep an alert episode's latest document for the next publish.

        An episode recorded under another flight id (a segment handover)
        moves to this one.
        """
        alert_id = str(doc.get("alert_id") or "")
        flight_id = str(doc.get("flight_id") or "")
        if not alert_id or not flight_id:
            return
        for other, episodes in self._episodes.items():
            if other != flight_id:
                episodes.pop(alert_id, None)
        self._episodes.setdefault(flight_id, {})[alert_id] = dict(doc)
        self._dirty_episodes.add(alert_id)

    def write_live_planes(self, planes: Mapping[str, Plane]) -> None:
        """Publish the tracked flights: one pipeline for every flight doc,
        telemetry point and episode that changed, and the deletion of every
        flight no longer tracked."""
        if not self.writer:
            return
        client = self._redis()
        if client is None:
            self._resync = True
            return
        current = {plane.flight_id: plane for plane in planes.values()}
        if self._resync:
            self._written_docs.clear()
            self._telemetry_through.clear()
            self._dirty_episodes.update(
                alert_id for docs in self._episodes.values() for alert_id in docs
            )
        pipe = client.pipeline(transaction=False)
        queued = False
        for flight_id, plane in current.items():
            queued = self._queue_flight(pipe, flight_id, plane) or queued
        gone = self._published - current.keys()
        for flight_id in list(self._episodes):
            if flight_id not in current and flight_id not in gone:
                # Never published (e.g. closed after its last publish, or a
                # previous process's flight closed at startup): nothing to do.
                del self._episodes[flight_id]
        for flight_id in gone:
            self._queue_delete(pipe, flight_id, self._episodes.pop(flight_id, {}))
            queued = True
        queued = self._queue_episodes(pipe) or queued
        try:
            if queued:
                pipe.execute()
        except RedisError as exc:
            log.error("Failed to publish %d live flight(s): %s", len(current), exc)
            # Possibly half applied: delete all of them next time if gone.
            self._published |= current.keys()
            self._resync = True
            self._mark_disconnected(exc, client)
            return
        self._published = set(current)
        self._dirty_episodes.clear()
        self._resync = False
        for flight_id in gone:
            self._written_docs.pop(flight_id, None)
            self._telemetry_through.pop(flight_id, None)

    def _queue_flight(self, pipe: Any, flight_id: str, plane: Plane) -> bool:
        """Queue what changed for one flight; returns whether anything was."""
        flight_doc = live_flight_doc(plane)
        encoded = json_dumps(flight_doc)
        queued = False
        if self._written_docs.get(flight_id) != encoded:
            queued = True
            pipe.sadd(KEY_FLIGHTS, flight_id)
            # end_time changes with every packet, so a tracked flight
            # re-arms its TTL long before it could lapse.
            pipe.set(
                KEY_FLIGHT.format(flight_id=flight_id),
                encoded,
                ex=LIVE_FLIGHT_TTL_SECONDS,
            )
            self._written_docs[flight_id] = encoded
        key = KEY_TELEMETRY.format(flight_id=flight_id)
        keep = self.telemetry_keep_seconds
        cutoff = time.time() - keep
        since = self._telemetry_through.get(flight_id)
        first = since is None
        if first:
            # First publish (or a republish): the ring is rebuilt from the
            # plane, so no stale or differently encoded point stays.
            pipe.delete(key)
            queued = True
            since = cutoff if keep > 0 else None
        samples = list(iter_telemetry_samples(plane, since=since))
        if first and not samples:
            # Keep the newest point even when it is older than the window.
            samples = list(iter_telemetry_samples(plane))[-1:]
        if samples:
            pipe.zadd(
                key,
                {
                    json_dumps(live_telemetry_point(plane, sample, plane.icao)): sample[
                        0
                    ]
                    for sample in samples
                },
            )
            pipe.expire(key, LIVE_FLIGHT_TTL_SECONDS)
            last = samples[-1][0]
            if keep > 0 and last >= cutoff:
                pipe.zremrangebyscore(key, "-inf", cutoff)
            self._telemetry_through[flight_id] = last
            queued = True
        elif first:
            self._telemetry_through[flight_id] = float("-inf")
        return queued

    def _queue_delete(
        self, pipe: Any, flight_id: str, episodes: Mapping[str, Any]
    ) -> None:
        pipe.srem(KEY_FLIGHTS, flight_id)
        pipe.delete(
            KEY_FLIGHT.format(flight_id=flight_id),
            KEY_TELEMETRY.format(flight_id=flight_id),
            KEY_ALERTS.format(flight_id=flight_id),
        )
        # An episode carried on to the next segment is now filed there.
        owned = [
            alert_id
            for alert_id in episodes
            if not any(alert_id in docs for docs in self._episodes.values())
        ]
        if owned:
            pipe.hdel(KEY_ACTIVE_ALERTS, *owned)
            pipe.hdel(KEY_ALERT_EPISODES, *owned)
            self._dirty_episodes.difference_update(owned)

    def _queue_episodes(self, pipe: Any) -> bool:
        any_queued = False
        for flight_id, episodes in self._episodes.items():
            alerts_key = KEY_ALERTS.format(flight_id=flight_id)
            queued = False
            for alert_id, doc in episodes.items():
                if alert_id not in self._dirty_episodes:
                    continue
                encoded = json_dumps(doc)
                if doc.get("active"):
                    pipe.hset(KEY_ACTIVE_ALERTS, alert_id, encoded)
                else:
                    pipe.hdel(KEY_ACTIVE_ALERTS, alert_id)
                pipe.hset(alerts_key, alert_id, encoded)
                pipe.hset(KEY_ALERT_EPISODES, alert_id, encoded)
                queued = True
            if queued:
                pipe.expire(alerts_key, LIVE_FLIGHT_TTL_SECONDS)
                any_queued = True
        return any_queued

    # -- episode outbox -------------------------------------------------------

    def save_outbox(self, entries: Mapping[str, Any]) -> None:
        """Keep episodes the archive refused (alert_id -> entry) in Redis."""
        client = self._redis() if self.writer else None
        if client is None or not entries:
            return
        try:
            pipe = client.pipeline()
            for alert_id, entry in entries.items():
                pipe.hset(KEY_EPISODE_OUTBOX, alert_id, json_dumps(entry))
            pipe.execute()
        except RedisError as exc:
            log.error("Failed to save %d unarchived episode(s): %s", len(entries), exc)
            self._mark_disconnected(exc, client)

    def drop_outbox(self, alert_ids: Iterable[str]) -> None:
        """Forget episodes the archive has accepted."""
        ids = list(alert_ids)
        client = self._redis() if self.writer else None
        if client is None or not ids:
            return
        try:
            client.hdel(KEY_EPISODE_OUTBOX, *ids)
        except RedisError as exc:
            log.warning("Failed to drop archived episodes from Redis: %s", exc)
            self._mark_disconnected(exc, client)

    def load_outbox(self) -> list[Any]:
        """Episodes a previous engine could not archive, as saved."""
        client = self._redis()
        if client is None:
            return []
        try:
            stored = cast("dict[str, str]", client.hgetall(KEY_EPISODE_OUTBOX))
        except RedisError as exc:
            log.error("Failed to read unarchived episodes: %s", exc)
            self._mark_disconnected(exc, client)
            return []
        entries = [safe_json_loads(raw) for raw in stored.values()]
        return [entry for entry in entries if isinstance(entry, list)]

    # -- clearing and maintenance ---------------------------------------------

    def clear_all(self) -> int:
        """Remove every live key, the heartbeat included (``pyaerial reset``).

        Returns how many live flights were listed (0 when Redis is down).
        """
        return self._clear(keep_engine=False)

    def clear_cache(self) -> int:
        """Remove the live flights, telemetry and episodes a previous engine
        process left (engine startup); this process's heartbeat and the
        episode outbox (not cache: the archive still needs it) stay."""
        return self._clear(keep_engine=True)

    def _clear(self, *, keep_engine: bool) -> int:
        self._published.clear()
        self._episodes.clear()
        self._resync = True
        client = self._redis()
        if client is None:
            return 0
        try:
            flight_ids = list(client.smembers(KEY_FLIGHTS))
            pipe = client.pipeline()
            for flight_id in flight_ids:
                pipe.delete(
                    KEY_FLIGHT.format(flight_id=flight_id),
                    KEY_TELEMETRY.format(flight_id=flight_id),
                    KEY_ALERTS.format(flight_id=flight_id),
                )
            shared = [KEY_FLIGHTS, KEY_ACTIVE_ALERTS, KEY_ALERT_EPISODES]
            owned = () if keep_engine else (KEY_ENGINE, KEY_EPISODE_OUTBOX)
            pipe.delete(*shared, *owned)
            pipe.execute()
            if flight_ids:
                log.info("Cleared %d stale live flight(s) from Redis.", len(flight_ids))
            return len(flight_ids)
        except RedisError as exc:
            log.warning("Could not clear Redis live store: %s", exc)
            return 0

    def pop_flight(self, flight_id: str) -> None:
        """Delete one flight's live keys and episodes (``pyaerial reset``).

        Raises :class:`~pyaerial.store.live.LiveUnavailable` when Redis is down.
        """
        client = self._client()
        try:
            flight_alerts = cast(
                "dict[str, str]", client.hgetall(KEY_ALERTS.format(flight_id=flight_id))
            )
            pipe = client.pipeline()
            self._queue_delete(pipe, flight_id, flight_alerts)
            pipe.execute()
        except RedisError as exc:
            self._failed(exc, client, "Failed to delete live flight %s: %s", flight_id)
        self._published.discard(flight_id)
        self._episodes.pop(flight_id, None)

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
