"""Clearing and popping for the engine's
:class:`~pyaerial.store.live.writer.LiveWriter`: dropping a previous
process's cache at startup, popping archived flights, and retrying failed
pops.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from typing import Any, cast

import redis
from redis.exceptions import RedisError

from pyaerial.jsonutil import dumps as json_dumps
from pyaerial.store.live._mirror import MirroredStore
from pyaerial.store.live.keys import (
    KEY_ACTIVE_ALERTS,
    KEY_ALERT_EPISODES,
    KEY_ALERTS,
    KEY_ENGINE,
    KEY_EPISODE_OUTBOX,
    KEY_FLIGHT,
    KEY_FLIGHTS,
    KEY_TELEMETRY,
    safe_json_loads,
)

log = logging.getLogger("pyaerial.store.redis")


class RecoveryMixin(MirroredStore):
    """Pops, pending pops and full clears."""

    def clear_all(self) -> int:
        """Remove every live key, the heartbeat included (``pyaerial reset``).

        Returns how many live flights were listed (0 when Redis is down).
        """
        return self._clear(keep_engine=False)

    def clear_cache(self) -> int:
        """Remove the live flights, telemetry and episodes a previous engine
        process left (engine startup); this process's heartbeat stays."""
        return self._clear(keep_engine=True)

    def _clear(self, *, keep_engine: bool) -> int:
        with self._mem_lock:
            seen_at = self._mem.engine_seen_at
            self._mem.clear()
            if keep_engine:
                self._mem.engine_seen_at = seen_at
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
            # The episode outbox is not cache: the next engine replays it.
            owned = () if keep_engine else (KEY_ENGINE, KEY_EPISODE_OUTBOX)
            pipe.delete(*shared, *owned)
            pipe.execute()
            self._pending_pops.clear()
            if flight_ids:
                log.info("Cleared %d stale live flight(s) from Redis.", len(flight_ids))
            return len(flight_ids)
        except RedisError as exc:
            log.warning("Could not clear Redis live store: %s", exc)
            return 0

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

    def flight_ids(self) -> set[str]:
        client = self._redis()
        if client is not None:
            try:
                return set(cast("set[str]", client.smembers(KEY_FLIGHTS)))
            except RedisError as exc:
                self._mark_disconnected(exc, client)
        return set(self._mem.flights)

    @staticmethod
    def _delete_redis_flight(client: redis.Redis, flight_id: str) -> dict[str, Any]:
        """Delete a flight's keys and index entries; returns what was stored."""
        raw_flight = client.get(KEY_FLIGHT.format(flight_id=flight_id))
        # decode_responses=True: replies are str.
        flight_alerts_raw = cast(
            "dict[str, str]", client.hgetall(KEY_ALERTS.format(flight_id=flight_id))
        )
        raw_alerts = list(flight_alerts_raw.values())
        active_raw = []
        active = cast("dict[str, str]", client.hgetall(KEY_ACTIVE_ALERTS))
        for alert_id, raw in active.items():
            parsed = safe_json_loads(raw)
            if isinstance(parsed, dict) and parsed.get("flight_id") == flight_id:
                active_raw.append(alert_id)
        # Only index entries still filed under this flight: an episode carried
        # on to the next segment keeps a copy here until this one is archived.
        episode_ids: set[str] = set()
        if flight_alerts_raw:
            ids = list(flight_alerts_raw)
            stored = cast("list[str | None]", client.hmget(KEY_ALERT_EPISODES, ids))
            for alert_id, episode in zip(ids, stored, strict=True):
                parsed = safe_json_loads(episode) if episode else None
                if not isinstance(parsed, dict) or parsed.get("flight_id") == flight_id:
                    episode_ids.add(alert_id)
        pipe = client.pipeline()
        pipe.srem(KEY_FLIGHTS, flight_id)
        pipe.delete(
            KEY_FLIGHT.format(flight_id=flight_id),
            KEY_TELEMETRY.format(flight_id=flight_id),
            KEY_ALERTS.format(flight_id=flight_id),
        )
        for alert_id in active_raw:
            pipe.hdel(KEY_ACTIVE_ALERTS, alert_id)
        if episode_ids:
            pipe.hdel(KEY_ALERT_EPISODES, *episode_ids)
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
