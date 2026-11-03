"""Finalize and recovery paths of the engine's
:class:`~pyaerial.store.live.writer.LiveWriter`: popping archived flights
(and retrying failed pops), rebuilding planes from live docs for the
archive, skipping unreadable docs, and cleaning up flights whose keys hit
their TTL.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import redis
from redis.exceptions import RedisError

from pyaerial.models import Plane
from pyaerial.store.codec import live_doc_icao, plane_from_live
from pyaerial.store.live._mirror import MirroredStore
from pyaerial.store.live.keys import (
    KEY_ACTIVE_ALERTS,
    KEY_ALERT_EPISODES,
    KEY_ALERTS,
    KEY_ENGINE,
    KEY_FLIGHT,
    KEY_FLIGHTS,
    KEY_TELEMETRY,
    LIVE_FLIGHT_TTL_SECONDS,
    safe_json_loads,
)

log = logging.getLogger("pyaerial.store.redis")

# Unreadable live flight docs remembered per process, so the orphan sweep does
# not re-read (and re-log) them every tick.
_UNREADABLE_FLIGHTS_MAX = 1_024


class RecoveryMixin(MirroredStore):
    """Pops, pending pops, finalize reads and full clears."""

    def clear_all(self) -> None:
        """Remove stale live keys from a previous engine session."""
        with self._mem_lock:
            self._mem.clear()
        client = self._redis()
        if client is None:
            return
        try:
            flight_ids = list(client.smembers(KEY_FLIGHTS))
            pipe = client.pipeline()
            for flight_id in flight_ids:
                pipe.delete(
                    KEY_FLIGHT.format(flight_id=flight_id),
                    KEY_TELEMETRY.format(flight_id=flight_id),
                    KEY_ALERTS.format(flight_id=flight_id),
                )
            pipe.delete(KEY_FLIGHTS, KEY_ACTIVE_ALERTS, KEY_ALERT_EPISODES, KEY_ENGINE)
            pipe.execute()
            self._pending_pops.clear()
            log.info("Cleared %d stale live flight(s) from Redis.", len(flight_ids))
        except RedisError as exc:
            log.warning("Could not clear Redis live store: %s", exc)

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
        key = KEY_FLIGHT.format(flight_id=flight_id)
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
                raw = client.get(KEY_FLIGHT.format(flight_id=flight_id))
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
            parsed = safe_json_loads(raw) if raw else None
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
        if all_episodes:
            episodes = cast("dict[str, str]", client.hgetall(KEY_ALERT_EPISODES))
            for alert_id, raw in episodes.items():
                parsed = safe_json_loads(raw)
                if isinstance(parsed, dict) and parsed.get("flight_id") == flight_id:
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
