"""Redis-backed live flight, telemetry, and alert buffer."""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from typing import Any

import redis
from redis.exceptions import RedisError

from pyaerial.constants import (
    ALERT_CAT_ETA,
    ALERT_CAT_REASON,
    ALERT_CAT_TYPE,
    ALERT_CAT_ZONE,
    STORE_ALT,
    STORE_CALLSIGN,
    STORE_FIRST_PACKET,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_MOST_RECENT_PACKET,
)
from pyaerial.store.mongo import flight_id_for_plane, iter_telemetry_samples

log = logging.getLogger("pyaerial.store.redis")

_KEY_FLIGHTS = "live:flights"
_KEY_FLIGHT = "live:flight:{flight_id}"
_KEY_TELEMETRY = "live:telemetry:{flight_id}"
_KEY_ALERTS = "live:alerts:{flight_id}"
_KEY_ACTIVE_ALERTS = "live:active_alerts"
_KEY_ALERT_EPISODES = "live:alert_episodes"
_RECONNECT_DELAY = 2.0


class RedisLiveStore:
    """Shared in-memory live store for engine writes and web portal reads."""

    def __init__(
        self,
        redis_uri: str,
        *,
        memory_only: bool = False,
        telemetry_keep_seconds: float = 600.0,
    ):
        self.uri = redis_uri
        self.memory_only = memory_only
        self.telemetry_keep_seconds = telemetry_keep_seconds
        self.client: redis.Redis | None = None
        self._last_telemetry_ts: dict[str, float] = {}
        self._mem_flights: dict[str, dict] = {}
        self._mem_telemetry: dict[str, list[dict]] = defaultdict(list)
        self._mem_alerts: dict[str, list[dict]] = defaultdict(list)
        self._mem_active_alerts: dict[str, dict] = {}
        self._mem_alert_episodes: list[dict] = []
        self._last_connect_attempt = 0.0
        self._last_ping_ok = 0.0
        self._reported_down = False
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
                self._backfill_redis_from_mem()
            else:
                log.info("Connected to Redis at %s", self.uri)
            self._reported_down = False
        except RedisError as exc:
            if not self._reported_down:
                log.warning(
                    "Redis unavailable at %s; operating with in-memory live "
                    "buffer. Reason: %s",
                    self.uri,
                    exc,
                )
                self._reported_down = True
            self.client = None

    def _ensure_connected(self) -> bool:
        if self.memory_only:
            return False
        if self.client is None:
            # Reconnect automatically (throttled) so a Redis that comes up after
            # startup -- or that recovers from an outage -- is picked up without
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
        except RedisError:
            log.warning("Lost Redis connection; operating with in-memory live buffer.")
            self.client.close()
            self.client = None
            return False

    def clear_all(self) -> None:
        """Remove stale live keys from a previous engine session."""
        self._mem_flights.clear()
        self._mem_telemetry.clear()
        self._mem_alerts.clear()
        self._mem_active_alerts.clear()
        self._mem_alert_episodes.clear()
        if not self._ensure_connected():
            return
        assert self.client is not None
        try:
            flight_ids = list(self.client.smembers(_KEY_FLIGHTS))
            if not flight_ids:
                return
            pipe = self.client.pipeline()
            for flight_id in flight_ids:
                pipe.delete(
                    _KEY_FLIGHT.format(flight_id=flight_id),
                    _KEY_TELEMETRY.format(flight_id=flight_id),
                    _KEY_ALERTS.format(flight_id=flight_id),
                )
            pipe.delete(_KEY_FLIGHTS, _KEY_ACTIVE_ALERTS, _KEY_ALERT_EPISODES)
            pipe.execute()
            log.info("Cleared %d stale live flight(s) from Redis.", len(flight_ids))
        except RedisError as exc:
            log.warning("Could not clear Redis live store: %s", exc)

    def write_live_planes(self, planes: dict[str, dict]) -> None:
        if not planes:
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
            self._mem_active_alerts[alert_id] = doc
        else:
            self._mem_active_alerts.pop(alert_id, None)

        flight_alerts = self._mem_alerts[flight_id]
        for i, existing in enumerate(flight_alerts):
            if existing.get("alert_id") == alert_id:
                flight_alerts[i] = doc
                break
        else:
            flight_alerts.append(doc)

        for i, existing in enumerate(self._mem_alert_episodes):
            if existing.get("alert_id") == alert_id:
                self._mem_alert_episodes[i] = doc
                break
        else:
            self._mem_alert_episodes.insert(0, doc)

        if not self._ensure_connected():
            return
        assert self.client is not None
        encoded = json.dumps(doc, separators=(",", ":"))
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
        existing = self._mem_active_alerts.get(alert_id)
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
        self._mem_active_alerts[alert_id] = doc

        flight_id = flight_id_for_plane(plane)
        flight_alerts = self._mem_alerts.get(flight_id, [])
        for i, item in enumerate(flight_alerts):
            if item.get("alert_id") == alert_id:
                flight_alerts[i] = doc
                break

        for i, item in enumerate(self._mem_alert_episodes):
            if item.get("alert_id") == alert_id:
                self._mem_alert_episodes[i] = doc
                break

        if not self._ensure_connected():
            return
        assert self.client is not None
        try:
            raw = self.client.hget(_KEY_ACTIVE_ALERTS, alert_id)
            if raw:
                stored = json.loads(raw)
                doc["activated_at"] = stored.get("activated_at", activated_at)
            pipe = self.client.pipeline()
            pipe.hset(
                _KEY_ACTIVE_ALERTS, alert_id, json.dumps(doc, separators=(",", ":"))
            )
            pipe.hset(
                _KEY_ALERTS.format(flight_id=flight_id),
                alert_id,
                json.dumps(doc, separators=(",", ":")),
            )
            pipe.hset(
                _KEY_ALERT_EPISODES,
                alert_id,
                json.dumps(doc, separators=(",", ":")),
            )
            pipe.execute()
        except RedisError as exc:
            log.error("Failed to update active alert %s: %s", alert_id, exc)

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
            "last_updated": activated_at,
            "position": {
                "type": "Point",
                "coordinates": [payload.get(STORE_LONG), payload.get(STORE_LAT)],
            },
            "altitude": payload.get(STORE_ALT),
        }

    def get_flights(self) -> list[dict[str, Any]]:
        if not self._ensure_connected():
            results: list[dict[str, Any]] = []
            for doc in self._mem_flights.values():
                last_tel = (
                    self._mem_telemetry[doc["flight_id"]][-1]
                    if self._mem_telemetry.get(doc["flight_id"])
                    else None
                )
                results.append(self._flight_summary(doc, last_tel))
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
                doc = json.loads(raw)
                last_tel = self._get_last_telemetry_point(flight_id)
                results.append(self._flight_summary(doc, last_tel))
        except RedisError as exc:
            log.error("Failed to read live flights from Redis: %s", exc)
        results.sort(key=lambda item: item.get("start_time") or 0, reverse=True)
        return results

    def get_flight(self, flight_id: str) -> dict[str, Any] | None:
        if not self._ensure_connected():
            doc = self._mem_flights.get(flight_id)
            return self._flight_detail(doc, flight_id) if doc else None
        assert self.client is not None
        try:
            raw = self.client.get(_KEY_FLIGHT.format(flight_id=flight_id))
            if not raw:
                return None
            return self._flight_detail(json.loads(raw), flight_id)
        except RedisError as exc:
            log.error("Failed to read live flight %s: %s", flight_id, exc)
            return None

    def get_telemetry(
        self, flight_id: str, *, since: float = 0.0
    ) -> list[dict[str, Any]]:
        if not self._ensure_connected():
            points = self._mem_telemetry.get(flight_id, [])
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
            return [json.loads(point) for point in raw_points]
        except RedisError as exc:
            log.error("Failed to read telemetry for %s: %s", flight_id, exc)
            return []

    def get_live_telemetry(self, since: float = 0.0) -> list[dict[str, Any]]:
        if not self._ensure_connected():
            points: list[dict[str, Any]] = []
            for flight_id, doc in self._mem_flights.items():
                active_alerts = doc.get("active_alerts") or []
                for point in self._mem_telemetry.get(flight_id, []):
                    if since > 0 and point.get("timestamp", 0) <= since:
                        continue
                    points.append(
                        {
                            "flight_id": flight_id,
                            "icao": point.get("icao"),
                            "active_alerts": active_alerts,
                            **point,
                        }
                    )
            points.sort(key=lambda item: item.get("timestamp") or 0)
            return points
        assert self.client is not None
        points = []
        try:
            for flight_id in self.client.smembers(_KEY_FLIGHTS):
                raw_flight = self.client.get(_KEY_FLIGHT.format(flight_id=flight_id))
                flight_doc = json.loads(raw_flight) if raw_flight else {}
                active_alerts = flight_doc.get("active_alerts") or []
                for point in self.get_telemetry(flight_id, since=since):
                    points.append(
                        {
                            "flight_id": flight_id,
                            "icao": point.get("icao"),
                            "active_alerts": active_alerts,
                            **point,
                        }
                    )
        except RedisError as exc:
            log.error("Failed to read live telemetry: %s", exc)
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
        if not self._ensure_connected():
            if active_only and not flight_id:
                alerts = list(self._mem_active_alerts.values())
            elif flight_id:
                if active_only:
                    alerts = [
                        a
                        for a in self._mem_active_alerts.values()
                        if a.get("flight_id") == flight_id
                    ]
                else:
                    alerts = list(self._mem_alerts.get(flight_id, []))
            else:
                alerts = list(self._mem_alert_episodes)
            if since:
                alerts = [
                    a
                    for a in alerts
                    if (a.get("activated_at") or a.get("last_updated") or 0) > since
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
                    raw_alerts = [
                        v
                        for v in self.client.hvals(_KEY_ACTIVE_ALERTS)
                        if json.loads(v).get("flight_id") == flight_id
                    ]
                else:
                    raw_alerts = self.client.hvals(
                        _KEY_ALERTS.format(flight_id=flight_id)
                    )
            else:
                raw_alerts = self.client.hvals(_KEY_ALERT_EPISODES)
            alerts = [json.loads(raw) for raw in raw_alerts]
            if since:
                alerts = [
                    alert
                    for alert in alerts
                    if (alert.get("activated_at") or alert.get("last_updated") or 0)
                    > since
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
            return []

    def pop_flight(self, flight_id: str) -> dict[str, Any]:
        """Return buffered flight data and delete Redis keys for the flight."""
        mem_flight = self._mem_flights.pop(flight_id, None)
        mem_alerts = self._mem_alerts.pop(flight_id, [])
        self._mem_telemetry.pop(flight_id, None)
        self._last_telemetry_ts.pop(flight_id, None)
        self._mem_alert_episodes = [
            a for a in self._mem_alert_episodes if a.get("flight_id") != flight_id
        ]
        for a in list(self._mem_active_alerts.values()):
            if a.get("flight_id") == flight_id:
                self._mem_active_alerts.pop(a["alert_id"], None)

        if not self._ensure_connected():
            return {
                "flight": mem_flight,
                "alerts": mem_alerts,
            }
        assert self.client is not None
        try:
            raw_flight = self.client.get(_KEY_FLIGHT.format(flight_id=flight_id))
            flight_alerts_raw = self.client.hgetall(
                _KEY_ALERTS.format(flight_id=flight_id)
            )
            raw_alerts = list(flight_alerts_raw.values())
            active_raw = {
                k: v
                for k, v in self.client.hgetall(_KEY_ACTIVE_ALERTS).items()
                if json.loads(v).get("flight_id") == flight_id
            }
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
            self._last_telemetry_ts.pop(flight_id, None)
            return {
                "flight": json.loads(raw_flight) if raw_flight else mem_flight,
                "alerts": [json.loads(raw) for raw in raw_alerts]
                if raw_alerts
                else mem_alerts,
            }
        except RedisError as exc:
            log.error("Failed to pop live flight %s: %s", flight_id, exc)
            return {"flight": mem_flight, "alerts": mem_alerts}

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    @staticmethod
    def _flight_detail(doc: dict[str, Any], flight_id: str) -> dict[str, Any]:
        info = doc.get("info", {})
        return {
            "flight_id": flight_id,
            "icao": doc.get("icao", ""),
            "active_alerts": doc.get("active_alerts", []),
            "start_time": doc.get("start_time"),
            "end_time": doc.get("end_time"),
            "callsign": doc.get("callsign") or info.get("callsign"),
            "model": doc.get("model") or info.get("model"),
            "owner": doc.get("owner") or info.get("owner"),
            "country": doc.get("country") or info.get("country"),
            "aircraft_type": doc.get("aircraft_type") or info.get("aircraft_type"),
            "is_live": True,
            "status": "live",
        }

    def _get_last_telemetry_point(self, flight_id: str) -> dict[str, Any] | None:
        if self.client is None:
            points = self._mem_telemetry.get(flight_id, [])
            return points[-1] if points else None
        key = _KEY_TELEMETRY.format(flight_id=flight_id)
        raw_points = self.client.zrevrange(key, 0, 0)
        if not raw_points:
            return None
        return json.loads(raw_points[0])

    def _flight_summary(
        self, doc: dict[str, Any], last_tel: dict[str, Any] | None
    ) -> dict[str, Any]:
        info = doc.get("info", {})
        lat = lon = alt = speed = heading = timestamp = None
        if last_tel:
            lat = last_tel.get("latitude")
            lon = last_tel.get("longitude")
            alt = last_tel.get("altitude")
            speed = last_tel.get("speed")
            heading = last_tel.get("heading")
            timestamp = last_tel.get("timestamp")
        flight_id = doc.get("flight_id") or doc.get("_id")
        return {
            "flight_id": flight_id,
            "icao": doc.get("icao", ""),
            "active_alerts": doc.get("active_alerts", []),
            "start_time": doc.get("start_time"),
            "end_time": doc.get("end_time"),
            "callsign": doc.get("callsign") or info.get("callsign"),
            "model": doc.get("model") or info.get("model"),
            "owner": doc.get("owner") or info.get("owner"),
            "country": doc.get("country") or info.get("country"),
            "aircraft_type": doc.get("aircraft_type") or info.get("aircraft_type"),
            "latitude": lat,
            "longitude": lon,
            "altitude": alt,
            "speed": speed,
            "heading": heading,
            "is_live": True,
            "status": "live",
            "retained": False,
            "timestamp": timestamp,
        }

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
            "info": {str(k): v for k, v in dict(info).items()},
        }
        self._mem_flights[flight_id] = flight_doc
        self._write_telemetry_points(plane, flight_id, icao)

        if not self._ensure_connected():
            return
        assert self.client is not None
        encoded = json.dumps(flight_doc, separators=(",", ":"))
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
            if self._ensure_connected() and self.client is not None
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

            self._mem_telemetry[flight_id].append(point)
            if pipe and key:
                pipe.zadd(
                    key, {json.dumps(point, separators=(",", ":")): timestamp}
                )
            last_written = max(last_written, timestamp)

        # Advance the cursor after the in-memory write so a failed Redis
        # execute does not duplicate mem points. Redis holes are repaired
        # by ``_backfill_redis_from_mem`` on reconnect.
        self._last_telemetry_ts[flight_id] = last_written
        keep = self.telemetry_keep_seconds
        if keep > 0:
            cutoff = time.time() - keep
            points = self._mem_telemetry[flight_id]
            trimmed = [point for point in points if point["timestamp"] >= cutoff]
            self._mem_telemetry[flight_id] = trimmed or points[-1:]
            if pipe and key:
                pipe.zremrangebyscore(key, "-inf", cutoff)
        if pipe:
            try:
                pipe.execute()
            except RedisError as exc:
                log.error("Failed to write live telemetry for %s: %s", flight_id, exc)

    def _backfill_redis_from_mem(self) -> None:
        """Replay in-memory flights/telemetry/alerts after a Redis reconnect."""
        if self.client is None:
            return
        try:
            pipe = self.client.pipeline()
            for flight_id, doc in self._mem_flights.items():
                pipe.sadd(_KEY_FLIGHTS, flight_id)
                pipe.set(
                    _KEY_FLIGHT.format(flight_id=flight_id),
                    json.dumps(doc, separators=(",", ":")),
                )
                key = _KEY_TELEMETRY.format(flight_id=flight_id)
                for point in self._mem_telemetry.get(flight_id, []):
                    ts = point.get("timestamp")
                    if ts is None:
                        continue
                    pipe.zadd(key, {json.dumps(point, separators=(",", ":")): ts})
            for alert_id, doc in self._mem_active_alerts.items():
                encoded = json.dumps(doc, separators=(",", ":"))
                pipe.hset(_KEY_ACTIVE_ALERTS, alert_id, encoded)
            for doc in self._mem_alert_episodes:
                alert_id = doc.get("alert_id")
                flight_id = doc.get("flight_id")
                if not alert_id:
                    continue
                encoded = json.dumps(doc, separators=(",", ":"))
                pipe.hset(_KEY_ALERT_EPISODES, alert_id, encoded)
                if flight_id:
                    pipe.hset(
                        _KEY_ALERTS.format(flight_id=flight_id), alert_id, encoded
                    )
            pipe.execute()
        except RedisError as exc:
            log.error("Failed to backfill Redis from memory: %s", exc)
