"""Redis-backed live flight, telemetry, and alert buffer."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import redis
from redis.exceptions import RedisError

from pyaerial.constants import (
    ALERT_CAT_ETA,
    ALERT_CAT_REASON,
    ALERT_CAT_TYPE,
    ALERT_CAT_ZONE,
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
from pyaerial.models import get_latest
from pyaerial.store.mongo import flight_id_for_plane

log = logging.getLogger("pyaerial.store.redis")

_KEY_FLIGHTS = "live:flights"
_KEY_FLIGHT = "live:flight:{flight_id}"
_KEY_TELEMETRY = "live:telemetry:{flight_id}"
_KEY_ALERTS = "live:alerts:{flight_id}"
_KEY_ACTIVE_ALERTS = "live:active_alerts"
_KEY_ALERT_EPISODES = "live:alert_episodes"
_RECONNECT_DELAY = 2.0


from collections import defaultdict

class RedisLiveStore:
    """Shared in-memory live store for engine writes and web portal reads."""

    def __init__(self, redis_uri: str):
        self.uri = redis_uri
        self.client: redis.Redis | None = None
        self._last_telemetry_ts: dict[str, float] = {}
        self._mem_flights: dict[str, dict] = {}
        self._mem_telemetry: dict[str, list[dict]] = defaultdict(list)
        self._mem_alerts: dict[str, list[dict]] = defaultdict(list)
        self._mem_active_alerts: dict[str, dict] = {}
        self._mem_alert_episodes: list[dict] = []
        self._connect()

    def _connect(self) -> None:
        try:
            self.client = redis.Redis.from_url(
                self.uri,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            self.client.ping()
            log.info("Connected to Redis at %s", self.uri)
        except RedisError as exc:
            log.info("Redis unavailable at %s; operating with in-memory live buffer.", self.uri)
            self.client = None

    def _ensure_connected(self) -> bool:
        if self.client is None:
            return False
        try:
            self.client.ping()
            return True
        except RedisError:
            log.warning("Lost Redis connection; operating with in-memory live buffer.")
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

    def record_alert_episode(self, plane: dict, meta: dict[str, Any], payload: dict[str, Any],
                             *, alert_id: str, activated_at: float,
                             active: bool = True,
                             deactivated_at: float | None = None) -> None:
        """Record alert activation/deactivation and update the live active set."""
        flight_id = flight_id_for_plane(plane)
        doc = self._alert_doc(plane, meta, payload, alert_id=alert_id,
                              activated_at=activated_at, active=active,
                              deactivated_at=deactivated_at)
        if active:
            self._mem_active_alerts[alert_id] = doc
        else:
            self._mem_active_alerts.pop(alert_id, None)
        self._mem_alerts[flight_id].append(doc)
        self._mem_alert_episodes.insert(0, doc)

        if not self._ensure_connected():
            return
        assert self.client is not None
        encoded = json.dumps(doc, separators=(",", ":"))
        try:
            pipe = self.client.pipeline()
            if active:
                pipe.hset(_KEY_ACTIVE_ALERTS, alert_id, encoded)
            else:
                pipe.hdel(_KEY_ACTIVE_ALERTS, alert_id)
            pipe.rpush(_KEY_ALERTS.format(flight_id=flight_id), encoded)
            pipe.lpush(_KEY_ALERT_EPISODES, encoded)
            pipe.execute()
        except RedisError as exc:
            log.error("Failed to record alert episode for %s: %s", flight_id, exc)

    def update_active_alert(self, plane: dict, alert_id: str, meta: dict[str, Any],
                            payload: dict[str, Any], timestamp: float) -> None:
        """Refresh ETA, position, and telemetry on an active alert."""
        existing = self._mem_active_alerts.get(alert_id)
        activated_at = existing.get("activated_at", timestamp) if existing else timestamp
        if existing is not None:
            doc = dict(existing)
            doc.update(self._alert_doc(
                plane, meta, payload,
                alert_id=alert_id,
                activated_at=activated_at,
                active=True,
                deactivated_at=None,
            ))
        else:
            doc = self._alert_doc(
                plane, meta, payload,
                alert_id=alert_id,
                activated_at=activated_at,
                active=True,
                deactivated_at=None,
            )
        doc["last_updated"] = timestamp
        self._mem_active_alerts[alert_id] = doc

        if not self._ensure_connected():
            return
        assert self.client is not None
        try:
            raw = self.client.hget(_KEY_ACTIVE_ALERTS, alert_id)
            if raw:
                stored = json.loads(raw)
                doc["activated_at"] = stored.get("activated_at", activated_at)
            self.client.hset(_KEY_ACTIVE_ALERTS, alert_id, json.dumps(doc, separators=(",", ":")))
        except RedisError as exc:
            log.error("Failed to update active alert %s: %s", alert_id, exc)

    def _alert_doc(self, plane: dict, meta: dict[str, Any], payload: dict[str, Any], *,
                   alert_id: str, activated_at: float, active: bool,
                   deactivated_at: float | None = None) -> dict[str, Any]:
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
                last_tel = self._mem_telemetry[doc["flight_id"]][-1] if self._mem_telemetry.get(doc["flight_id"]) else None
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
            if not doc:
                return None
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
                "aircraft_type": doc.get("aircraft_type") or info.get("aircraft_type") or info.get("typecode"),
                "flight_phase": doc.get("flight_phase") or info.get("flight_phase"),
                "raw_messages": doc.get("raw_messages", []),
                "is_live": True,
                "status": "live",
            }
        assert self.client is not None
        try:
            raw = self.client.get(_KEY_FLIGHT.format(flight_id=flight_id))
            if not raw:
                return None
            doc = json.loads(raw)
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
                "aircraft_type": doc.get("aircraft_type") or info.get("aircraft_type") or info.get("typecode"),
                "flight_phase": doc.get("flight_phase") or info.get("flight_phase"),
                "raw_messages": doc.get("raw_messages", []),
                "is_live": True,
                "status": "live",
            }
        except RedisError as exc:
            log.error("Failed to read live flight %s: %s", flight_id, exc)
            return None

    def get_telemetry(self, flight_id: str, *, since: float = 0.0) -> list[dict[str, Any]]:
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
                    points.append({
                        "flight_id": flight_id,
                        "icao": point.get("icao"),
                        "active_alerts": active_alerts,
                        **point,
                    })
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
                    points.append({
                        "flight_id": flight_id,
                        "icao": point.get("icao"),
                        "active_alerts": active_alerts,
                        **point,
                    })
        except RedisError as exc:
            log.error("Failed to read live telemetry: %s", exc)
        points.sort(key=lambda item: item.get("timestamp") or 0)
        return points

    def get_alerts(self, *, since: float = 0.0, flight_id: str | None = None,
                   rule: str | None = None, active_only: bool = True) -> list[dict[str, Any]]:
        if not self._ensure_connected():
            if active_only and not flight_id:
                alerts = list(self._mem_active_alerts.values())
            elif flight_id:
                if active_only:
                    alerts = [a for a in self._mem_active_alerts.values() if a.get("flight_id") == flight_id]
                else:
                    alerts = list(self._mem_alerts.get(flight_id, []))
            else:
                alerts = list(self._mem_alert_episodes)
            if since:
                alerts = [a for a in alerts if (a.get("activated_at") or a.get("last_updated") or 0) > since]
            if rule:
                alerts = [a for a in alerts if a.get("rule") == rule]
            if active_only:
                alerts = [a for a in alerts if a.get("active", True)]
            alerts.sort(key=lambda item: item.get("activated_at") or item.get("last_updated") or 0, reverse=True)
            return alerts
        assert self.client is not None
        try:
            if active_only and not flight_id:
                raw_alerts = self.client.hvals(_KEY_ACTIVE_ALERTS)
            elif flight_id:
                if active_only:
                    raw_alerts = [
                        v for v in self.client.hvals(_KEY_ACTIVE_ALERTS)
                        if json.loads(v).get("flight_id") == flight_id
                    ]
                else:
                    raw_alerts = self.client.lrange(_KEY_ALERTS.format(flight_id=flight_id), 0, -1)
            else:
                raw_alerts = self.client.lrange(_KEY_ALERT_EPISODES, 0, -1)
            alerts = [json.loads(raw) for raw in raw_alerts]
            if since:
                alerts = [
                    alert for alert in alerts
                    if (alert.get("activated_at") or alert.get("last_updated") or 0) > since
                ]
            if rule:
                alerts = [alert for alert in alerts if alert.get("rule") == rule]
            if active_only:
                alerts = [alert for alert in alerts if alert.get("active", True)]
            alerts.sort(
                key=lambda item: item.get("activated_at") or item.get("last_updated") or 0,
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
            raw_alerts = self.client.lrange(_KEY_ALERTS.format(flight_id=flight_id), 0, -1)
            active_raw = {
                k: v for k, v in self.client.hgetall(_KEY_ACTIVE_ALERTS).items()
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
            for alert_raw in raw_alerts:
                pipe.lrem(_KEY_ALERT_EPISODES, 0, alert_raw)
            pipe.execute()
            self._last_telemetry_ts.pop(flight_id, None)
            return {
                "flight": json.loads(raw_flight) if raw_flight else mem_flight,
                "alerts": [json.loads(raw) for raw in raw_alerts] if raw_alerts else mem_alerts,
            }
        except RedisError as exc:
            log.error("Failed to pop live flight %s: %s", flight_id, exc)
            return {"flight": mem_flight, "alerts": mem_alerts}

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def _get_last_telemetry_point(self, flight_id: str) -> dict[str, Any] | None:
        if self.client is None:
            points = self._mem_telemetry.get(flight_id, [])
            return points[-1] if points else None
        key = _KEY_TELEMETRY.format(flight_id=flight_id)
        raw_points = self.client.zrevrange(key, 0, 0)
        if not raw_points:
            return None
        return json.loads(raw_points[0])

    def _flight_summary(self, doc: dict[str, Any], last_tel: dict[str, Any] | None) -> dict[str, Any]:
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
            "aircraft_type": doc.get("aircraft_type") or info.get("aircraft_type") or info.get("typecode"),
            "flight_phase": doc.get("flight_phase") or info.get("flight_phase"),
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
            "aircraft_type": info.get("aircraft_type") or info.get("typecode") or "",
            "typecode": info.get("typecode") or "",
            "flight_phase": info.get("flight_phase") or "",
            "info": {str(k): v for k, v in info.items()},
            "raw_messages": plane.get("raw_messages", []),
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
        lat_series = plane.get(STORE_RECV_DATA, {}).get(STORE_LAT, [])
        if not lat_series:
            return

        wrote = False
        pipe = self.client.pipeline() if self._ensure_connected() and self.client is not None else None
        key = _KEY_TELEMETRY.format(flight_id=flight_id) if pipe else None

        for lat_datum in lat_series:
            if lat_datum.time <= last_written:
                continue
            lon_datum = get_latest(STORE_RECV_DATA, STORE_LONG, plane, lat_datum.time)
            if lon_datum is None:
                continue
            alt_datum = get_latest(STORE_RECV_DATA, STORE_ALT, plane, lat_datum.time)
            speed_datum = (
                get_latest(STORE_CALC_DATA, STORE_HORIZ_SPEED, plane, lat_datum.time)
                or get_latest(STORE_RECV_DATA, STORE_HORIZ_SPEED, plane, lat_datum.time)
            )
            heading_datum = (
                get_latest(STORE_CALC_DATA, STORE_HEADING, plane, lat_datum.time)
                or get_latest(STORE_RECV_DATA, STORE_HEADING, plane, lat_datum.time)
            )
            point: dict[str, Any] = {
                "icao": icao,
                "timestamp": lat_datum.time,
                "latitude": lat_datum.value,
                "longitude": lon_datum.value,
            }
            if alt_datum is not None:
                point["altitude"] = alt_datum.value
            if speed_datum is not None:
                point["speed"] = speed_datum.value
            if heading_datum is not None:
                point["heading"] = heading_datum.value

            self._mem_telemetry[flight_id].append(point)
            if pipe and key:
                pipe.zadd(key, {json.dumps(point, separators=(",", ":")): lat_datum.time})
            last_written = max(last_written, lat_datum.time)
            wrote = True

        if not wrote:
            return
        self._last_telemetry_ts[flight_id] = last_written
        if pipe:
            try:
                pipe.execute()
            except RedisError as exc:
                log.error("Failed to write live telemetry for %s: %s", flight_id, exc)
