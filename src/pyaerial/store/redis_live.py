"""Redis storage backend for active flight state, telemetry rings, and alert episodes."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

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
        self.memory_only = memory_only
        self.telemetry_keep_seconds = telemetry_keep_seconds
        # Only the tracking engine owns Redis live keys. Portal/CLI readers
        # reconnect without deleting flights that are missing from their empty mem.
        self.writer = writer
        self.client: redis.Redis | None = None
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
        in-memory buffer and retries later via :meth:`_ensure_connected`.
        """
        try:
            self.client = redis.Redis.from_url(
                self.uri,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            self.client.ping()
            if self._reported_down:
                log.info("Reconnected to Redis at %s", self.uri)
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
                log.info("Connected to Redis at %s", self.uri)
            self._reported_down = False
        except RedisError as exc:
            self._mark_disconnected(exc)

    def _mark_disconnected(self, exc: BaseException | None = None) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        if not self._reported_down:
            self._reported_down = True
            if exc is not None:
                log.warning(
                    "Redis unavailable at %s; operating with in-memory live "
                    "buffer. Reason: %s",
                    self.uri,
                    exc,
                )

    def _ensure_connected(self) -> bool:
        if self.memory_only:
            return False
        if self.client is None:
            # Reconnect automatically (throttled) so a Redis that comes up after
            # startup, or that recovers from an outage, is picked up without
            # restarting the process.
            now = time.monotonic()
            if now - self._last_connect_attempt >= _RECONNECT_DELAY:
                self._last_connect_attempt = now
                try:
                    self._connect()
                except Exception:  # pragma: no cover - defensive
                    log.debug("Redis reconnect attempt failed", exc_info=True)
                    self.client = None
        if self.client is None:
            return False
        now = time.monotonic()
        if now - self._last_ping_ok < _RECONNECT_DELAY:
            return True
        try:
            self.client.ping()
            self._last_ping_ok = now
            return True
        except RedisError as exc:
            log.warning("Lost Redis connection; operating with in-memory live buffer.")
            self._mark_disconnected(exc)
            return False

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
        if not self._ensure_connected():
            return
        assert self.client is not None
        payload = _engine_heartbeat_payload(self._engine_token, now)
        try:
            raw = self.client.get(_KEY_ENGINE)
            doc = _safe_json_loads(raw) if raw else None
            if (
                raw
                and not self._owns_heartbeat(doc)
                and self._heartbeat_is_fresh(doc, now)
            ):
                return
            self.client.set(_KEY_ENGINE, payload, ex=LIVE_ENGINE_TTL_SECONDS)
        except RedisError as exc:
            log.error("Failed to write engine heartbeat: %s", exc)

    def engine_seen_at(self) -> float | None:
        """Unix timestamp of the last engine heartbeat, or None if missing."""
        if self._ensure_connected():
            assert self.client is not None
            try:
                raw = self.client.get(_KEY_ENGINE)
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
        if not self._ensure_connected() or self.client is None:
            return False
        try:
            raw = self.client.get(_KEY_ENGINE)
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
        if not self._ensure_connected() or self.client is None:
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
        if not self._ensure_connected() or self.client is None:
            return False
        payload = _engine_heartbeat_payload(self._engine_token, now)
        try:
            if self.client.set(
                _KEY_ENGINE, payload, ex=LIVE_ENGINE_TTL_SECONDS, nx=True
            ):
                return True
            raw = self.client.get(_KEY_ENGINE)
            doc = _safe_json_loads(raw) if raw else None
            if self._owns_heartbeat(doc):
                self.client.set(_KEY_ENGINE, payload, ex=LIVE_ENGINE_TTL_SECONDS)
                return True
            if self._heartbeat_is_fresh(doc, now):
                return False
            self.client.set(_KEY_ENGINE, payload, ex=LIVE_ENGINE_TTL_SECONDS)
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
        if not self._ensure_connected():
            return
        assert self.client is not None
        try:
            raw = self.client.get(_KEY_ENGINE)
            doc = _safe_json_loads(raw) if raw else None
            if raw and not self._owns_heartbeat(doc):
                return
            self.client.delete(_KEY_ENGINE)
        except RedisError as exc:
            log.debug("Could not clear engine heartbeat: %s", exc)

    def clear_all(self) -> None:
        """Remove stale live keys from a previous engine session."""
        self._mem.clear()
        if not self._ensure_connected():
            return
        assert self.client is not None
        try:
            flight_ids = list(self.client.smembers(_KEY_FLIGHTS))
            pipe = self.client.pipeline()
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

        if not self.writer or not self._ensure_connected():
            return
        assert self.client is not None
        encoded = json_dumps(doc)
        try:
            pipe = self.client.pipeline()
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

        if not self.writer or not self._ensure_connected():
            return
        assert self.client is not None
        try:
            raw = self.client.hget(_KEY_ACTIVE_ALERTS, alert_id)
            if raw:
                stored = _safe_json_loads(raw)
                if isinstance(stored, dict):
                    doc["activated_at"] = stored.get("activated_at", activated_at)
            pipe = self.client.pipeline()
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
            self._mark_disconnected(exc)

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
        if not self._ensure_connected():
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
        assert self.client is not None
        results = []
        try:
            flight_ids = sorted(self.client.smembers(_KEY_FLIGHTS))
            for flight_id in flight_ids:
                raw = self.client.get(_KEY_FLIGHT.format(flight_id=flight_id))
                if not raw:
                    continue
                doc = _safe_json_loads(raw)
                if not isinstance(doc, dict):
                    continue
                last_tel = self._get_last_telemetry_point(flight_id)
                results.append(live_flight_summary(doc, last_tel))
        except RedisError as exc:
            log.error("Failed to read live flights from Redis: %s", exc)
            self._mark_disconnected(exc)
            self._raise_if_reader_disconnected()
        results.sort(key=lambda item: item.get("start_time") or 0, reverse=True)
        return results

    def get_flight(self, flight_id: str) -> dict[str, Any] | None:
        if not self._ensure_connected():
            self._raise_if_reader_disconnected()
            doc = self._mem.flights.get(flight_id)
            return live_flight_detail(doc, flight_id) if doc else None
        assert self.client is not None
        try:
            raw = self.client.get(_KEY_FLIGHT.format(flight_id=flight_id))
            if not raw:
                return None
            doc = _safe_json_loads(raw)
            if not isinstance(doc, dict):
                return None
            return live_flight_detail(doc, flight_id)
        except RedisError as exc:
            log.error("Failed to read live flight %s: %s", flight_id, exc)
            self._mark_disconnected(exc)
            self._raise_if_reader_disconnected()
            return None

    def get_telemetry(
        self, flight_id: str, *, since: float = 0.0
    ) -> list[dict[str, Any]]:
        if not self._ensure_connected():
            self._raise_if_reader_disconnected()
            points = self._mem.telemetry.get(flight_id, [])
            if since > 0:
                points = [p for p in points if p.get("timestamp", 0) > since]
            return points
        assert self.client is not None
        key = _KEY_TELEMETRY.format(flight_id=flight_id)
        try:
            if since > 0:
                raw_points = self.client.zrangebyscore(key, f"({since}", "+inf")
            else:
                raw_points = self.client.zrange(key, 0, -1)
            points = []
            for point in raw_points:
                parsed = _safe_json_loads(point)
                if isinstance(parsed, dict):
                    points.append(parsed)
            return points
        except RedisError as exc:
            log.error("Failed to read telemetry for %s: %s", flight_id, exc)
            self._mark_disconnected(exc)
            self._raise_if_reader_disconnected()
            return []

    def get_live_telemetry(self, since: float = 0.0) -> list[dict[str, Any]]:
        if not self._ensure_connected():
            self._raise_if_reader_disconnected()
            points: list[dict[str, Any]] = []
            for flight_id in self._mem.flights:
                for point in self._mem.telemetry.get(flight_id, []):
                    if since > 0 and point.get("timestamp", 0) <= since:
                        continue
                    points.append(
                        {
                            "flight_id": flight_id,
                            "icao": point.get("icao"),
                            "timestamp": point.get("timestamp"),
                            "latitude": point.get("latitude"),
                            "longitude": point.get("longitude"),
                            "altitude": point.get("altitude"),
                            "speed": point.get("speed"),
                            "heading": point.get("heading"),
                        }
                    )
            points.sort(key=lambda item: item.get("timestamp") or 0)
            return points
        assert self.client is not None
        points = []
        try:
            for flight_id in self.client.smembers(_KEY_FLIGHTS):
                for point in self.get_telemetry(flight_id, since=since):
                    points.append(
                        {
                            "flight_id": flight_id,
                            "icao": point.get("icao"),
                            "timestamp": point.get("timestamp"),
                            "latitude": point.get("latitude"),
                            "longitude": point.get("longitude"),
                            "altitude": point.get("altitude"),
                            "speed": point.get("speed"),
                            "heading": point.get("heading"),
                        }
                    )
        except RedisError as exc:
            log.error("Failed to read live telemetry: %s", exc)
            self._mark_disconnected(exc)
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
        if not self._ensure_connected() or mem_has_flight:
            if not self._ensure_connected():
                self._raise_if_reader_disconnected()
            if active_only and not flight_id:
                alerts = list(self._mem.active_alerts.values())
            elif flight_id:
                if active_only:
                    alerts = [
                        a
                        for a in self._mem.active_alerts.values()
                        if a.get("flight_id") == flight_id
                    ]
                else:
                    alerts = list(self._mem.alerts.get(flight_id, []))
            else:
                alerts = list(self._mem.alert_episodes)
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
                key=lambda item: (
                    item.get("activated_at") or item.get("last_updated") or 0
                ),
                reverse=True,
            )
            return alerts
        assert self.client is not None
        try:
            if active_only and not flight_id:
                raw_alerts = self.client.hvals(_KEY_ACTIVE_ALERTS)
            elif flight_id:
                if active_only:
                    raw_alerts = []
                    for value in self.client.hvals(_KEY_ACTIVE_ALERTS):
                        parsed = _safe_json_loads(value)
                        if (
                            isinstance(parsed, dict)
                            and parsed.get("flight_id") == flight_id
                        ):
                            raw_alerts.append(value)
                else:
                    raw_alerts = self.client.hvals(
                        _KEY_ALERTS.format(flight_id=flight_id)
                    )
            else:
                raw_alerts = self.client.hvals(_KEY_ALERT_EPISODES)
            alerts = []
            for raw in raw_alerts:
                try:
                    alerts.append(json.loads(raw))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
            if since:
                alerts = [
                    alert
                    for alert in alerts
                    if (alert.get("activated_at") or alert.get("last_updated") or 0)
                    >= since
                ]
            if rule:
                alerts = [alert for alert in alerts if alert.get("rule") == rule]
            if active_only:
                alerts = [alert for alert in alerts if alert.get("active", True)]
            alerts.sort(
                key=lambda item: (
                    item.get("activated_at") or item.get("last_updated") or 0
                ),
                reverse=True,
            )
            return alerts
        except RedisError as exc:
            log.error("Failed to read live alerts: %s", exc)
            self._mark_disconnected(exc)
            self._raise_if_reader_disconnected()
            return []

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

        if not self._ensure_connected():
            self._pending_pops.add(flight_id)
            return {
                "flight": mem_flight,
                "alerts": mem_alerts,
            }
        assert self.client is not None
        try:
            snapshot = self._delete_redis_flight(flight_id)
            self._pending_pops.discard(flight_id)
            return {
                "flight": snapshot.get("flight") or mem_flight,
                "alerts": snapshot.get("alerts") or mem_alerts,
            }
        except RedisError as exc:
            log.error("Failed to pop live flight %s: %s", flight_id, exc)
            self._pending_pops.add(flight_id)
            self._mark_disconnected(exc)
            return {"flight": mem_flight, "alerts": mem_alerts}

    def retry_pending_pops(self) -> None:
        if not self.writer or not self._pending_pops:
            return
        if not self._ensure_connected():
            return
        assert self.client is not None
        for flight_id in list(self._pending_pops):
            try:
                self._delete_redis_flight(flight_id)
                self._pending_pops.discard(flight_id)
            except RedisError as exc:
                self._mark_disconnected(exc)
                return

    def flight_ids(self) -> set[str]:
        if self._ensure_connected() and self.client is not None:
            try:
                return set(self.client.smembers(_KEY_FLIGHTS))
            except RedisError as exc:
                self._mark_disconnected(exc)
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
        if doc is None and self._ensure_connected() and self.client is not None:
            try:
                raw = self.client.get(_KEY_FLIGHT.format(flight_id=flight_id))
            except RedisError as exc:
                self._mark_disconnected(exc)
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

    def _delete_redis_flight(self, flight_id: str) -> dict[str, Any]:
        assert self.client is not None
        raw_flight = self.client.get(_KEY_FLIGHT.format(flight_id=flight_id))
        flight_alerts_raw = self.client.hgetall(_KEY_ALERTS.format(flight_id=flight_id))
        raw_alerts = list(flight_alerts_raw.values())
        active_raw = []
        for alert_id, raw in self.client.hgetall(_KEY_ACTIVE_ALERTS).items():
            try:
                if json.loads(raw).get("flight_id") == flight_id:
                    active_raw.append(alert_id)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        pipe = self.client.pipeline()
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
        if not self._ensure_connected():
            return
        assert self.client is not None
        try:
            self.client.publish(_RAW_CHANNEL, json_dumps(payload))
        except RedisError as exc:
            log.debug("Failed to publish raw frames: %s", exc)

    def add_raw_listener(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Register an in-process listener for :meth:`publish_raw`."""
        self._raw_listeners.append(callback)

        def remove() -> None:
            try:
                self._raw_listeners.remove(callback)
            except ValueError:
                pass

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
            try:
                self._raw_listeners.remove(callback)
            except ValueError:
                pass
        self._raw_stop.set()
        pubsub = self._raw_pubsub
        self._raw_pubsub = None
        if pubsub is not None:
            try:
                pubsub.close()
            except Exception:
                pass
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
                    except Exception:
                        pass
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
            if self._raw_stop.wait(_RAW_PUBSUB_RETRY):
                return

    def close(self) -> None:
        self.stop_raw_pubsub()
        if self.client is not None:
            self.client.close()
            self.client = None

    def _get_last_telemetry_point(self, flight_id: str) -> dict[str, Any] | None:
        if self.client is None:
            points = self._mem.telemetry.get(flight_id, [])
            return points[-1] if points else None
        key = _KEY_TELEMETRY.format(flight_id=flight_id)
        raw_points = self.client.zrevrange(key, 0, 0)
        if not raw_points:
            return None
        parsed = _safe_json_loads(raw_points[0])
        return parsed if isinstance(parsed, dict) else None

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

        if not self.writer or not self._ensure_connected():
            return
        assert self.client is not None
        encoded = json_dumps(flight_doc)
        try:
            pipe = self.client.pipeline()
            pipe.sadd(_KEY_FLIGHTS, flight_id)
            pipe.set(_KEY_FLIGHT.format(flight_id=flight_id), encoded)
            pipe.execute()
        except RedisError as exc:
            log.error("Failed to upsert live flight %s: %s", flight_id, exc)

    def _write_telemetry_points(self, plane: dict, flight_id: str, icao: str) -> None:
        last_written = self._last_telemetry_ts.get(flight_id, 0.0)
        samples = [
            sample
            for sample in iter_telemetry_samples(plane)
            if sample[0] > last_written
        ]
        if not samples:
            return

        pipe = (
            self.client.pipeline()
            if self.writer and self._ensure_connected() and self.client is not None
            else None
        )
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
                self._mark_disconnected(exc)

    def _backfill_redis_from_mem(self) -> None:
        """Replay in-memory flights/telemetry/alerts after a Redis reconnect."""
        if not self.writer or self.client is None:
            return
        try:
            # Only delete keys this process already decided to pop. Redis ids
            # that are not in mem may be leftovers from a previous engine and
            # must be archived by the engine, not wiped here.
            for flight_id in list(self._pending_pops):
                try:
                    self._delete_redis_flight(flight_id)
                    self._pending_pops.discard(flight_id)
                except RedisError as exc:
                    log.debug(
                        "Could not drop pending Redis flight %s: %s", flight_id, exc
                    )
            pipe = self.client.pipeline()
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
