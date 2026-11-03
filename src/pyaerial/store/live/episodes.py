"""Alert episodes in the live store, for the engine's
:class:`~pyaerial.store.live.writer.LiveWriter`.

One document per ``alert_id`` in the per-flight ``live:alerts:{flight_id}``
hash, the shared ``live:alert_episodes`` index and, while active,
``live:active_alerts``. Active refreshes are queued for the tick's
:meth:`~LiveWriter.write_live_planes` pipeline.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from redis.exceptions import RedisError

from pyaerial.jsonutil import dumps as json_dumps
from pyaerial.models import Plane
from pyaerial.store.codec import alert_episode_doc
from pyaerial.store.live._conn import LiveUnavailable
from pyaerial.store.live._mirror import MirroredStore
from pyaerial.store.live._reads import alerts_by_id, filter_alerts, read_alerts
from pyaerial.store.live.keys import (
    KEY_ACTIVE_ALERTS,
    KEY_ALERT_EPISODES,
    KEY_ALERTS,
    KEY_FLIGHTS,
    LIVE_FLIGHT_TTL_SECONDS,
    safe_json_loads,
)

log = logging.getLogger("pyaerial.store.redis")


class EpisodesMixin(MirroredStore):
    """Record, refresh, carry and read alert episodes."""

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
            alerts_key = KEY_ALERTS.format(flight_id=flight_id)
            if active:
                pipe.hset(KEY_ACTIVE_ALERTS, alert_id, encoded)
            else:
                pipe.hdel(KEY_ACTIVE_ALERTS, alert_id)
            # Both per-flight alerts and the shared episode index are hashes
            # keyed by alert_id, so upserts are O(1) instead of a list scan.
            pipe.hset(alerts_key, alert_id, encoded)
            pipe.expire(alerts_key, LIVE_FLIGHT_TTL_SECONDS)
            pipe.hset(KEY_ALERT_EPISODES, alert_id, encoded)
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
            alerts_key = KEY_ALERTS.format(flight_id=flight_id)
            pipe.hset(KEY_ACTIVE_ALERTS, alert_id, encoded)
            pipe.hset(alerts_key, alert_id, encoded)
            pipe.expire(alerts_key, LIVE_FLIGHT_TTL_SECONDS)
            pipe.hset(KEY_ALERT_EPISODES, alert_id, encoded)
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
            new_key = KEY_ALERTS.format(flight_id=new_flight_id)
            for alert_id, doc in moved.items():
                encoded = json_dumps(doc)
                pipe.hset(KEY_ACTIVE_ALERTS, alert_id, encoded)
                pipe.hset(KEY_ALERT_EPISODES, alert_id, encoded)
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
        return alert_episode_doc(
            plane,
            meta,
            payload,
            alert_id=alert_id,
            activated_at=activated_at,
            active=active,
            deactivated_at=deactivated_at,
        )

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
        # Memory's latest state of every episode it holds (including closed
        # ones, and an old segment's copy of a carried episode).
        mem_all = alerts_by_id(self._mem_alerts(flight_id, active_only=False))
        # What memory contributes: for active_only only its active set, so a
        # carried episode's old-segment copy is never reported as active there.
        mem_docs = (
            alerts_by_id(self._mem_alerts(flight_id, active_only=True))
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
                redis_docs = read_alerts(client, flight_id, active_only)
            except RedisError as exc:
                log.error("Failed to read live alerts: %s", exc)
                self._mark_disconnected(exc, client)
                if self.writer and not self.memory_only:
                    raise LiveUnavailable(f"could not read live alerts: {exc}") from exc
                self._raise_if_reader_disconnected()
                return []
            merged = alerts_by_id(redis_docs)
            merged.update(mem_docs)
            # Memory wins for episodes it knows, e.g. a deactivation whose
            # Redis write failed must not resurrect as active.
            alerts = [mem_all.get(key, doc) for key, doc in merged.items()]
        return filter_alerts(alerts, since=since, rule=rule, active_only=active_only)

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
            live = set(client.smembers(KEY_FLIGHTS))
            active = cast("dict[str, str]", client.hgetall(KEY_ACTIVE_ALERTS))
            strays: list[dict[str, Any]] = []
            broken: list[str] = []
            for alert_id, raw in active.items():
                doc = safe_json_loads(raw)
                flight_id = doc.get("flight_id") if isinstance(doc, dict) else None
                if not isinstance(flight_id, str) or "-" not in flight_id:
                    broken.append(alert_id)
                elif flight_id not in live:
                    strays.append({**doc, "alert_id": alert_id})
            if broken and self.writer:
                log.warning("Dropping %d unreadable active alert(s)", len(broken))
                client.hdel(KEY_ACTIVE_ALERTS, *broken)
        except RedisError as exc:
            log.error("Failed to read active alerts: %s", exc)
            self._mark_disconnected(exc, client)
            return []
        return strays
