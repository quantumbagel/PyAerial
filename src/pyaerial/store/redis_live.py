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
_KEY_ALERTS_RECENT = "live:alerts:recent"
_RECONNECT_DELAY = 2.0


class RedisLiveStore:
    """Shared in-memory live store for engine writes and web portal reads."""

    def __init__(self, redis_uri: str):
        self.uri = redis_uri
        self.client: redis.Redis | None = None
        self._last_telemetry_ts: dict[str, float] = {}
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
            log.error("Could not reach Redis at %s: %s", self.uri, exc)
            self.client = None

    def _ensure_connected(self) -> bool:
        if self.client is None:
            self._connect()
            return self.client is not None
        try:
            self.client.ping()
            return True
        except RedisError:
            log.warning("Lost Redis connection; attempting to reconnect...")
            try:
                self._connect()
                if self.client is not None:
                    self.client.ping()
                    return True
            except RedisError:
                log.error("Reconnect to Redis failed.")
                time.sleep(_RECONNECT_DELAY)
            return False

    def clear_all(self) -> None:
        """Remove stale live keys from a previous engine session."""
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
            pipe.delete(_KEY_FLIGHTS, _KEY_ALERTS_RECENT)
            pipe.execute()
            log.info("Cleared %d stale live flight(s) from Redis.", len(flight_ids))
        except RedisError as exc:
            log.warning("Could not clear Redis live store: %s", exc)

    def write_live_planes(self, planes: dict[str, dict]) -> None:
        if not planes or not self._ensure_connected():
            return
        assert self.client is not None
        for plane in planes.values():
            self._upsert_live_flight(plane)

    def record_alert(self, plane: dict, meta: dict[str, Any], payload: dict[str, Any],
                     timestamp: float | None = None) -> None:
        if not self._ensure_connected():
            return
        assert self.client is not None
        flight_id = flight_id_for_plane(plane)
        event_time = timestamp or time.time()
        doc = {
            "alert_id": f"{flight_id}:{event_time:.6f}",
            "flight_id": flight_id,
            "icao": meta[STORE_ICAO].lower(),
            "callsign": meta.get(STORE_CALLSIGN) or "",
            "zone": meta.get(ALERT_CAT_ZONE, ""),
            "level": meta.get(ALERT_CAT_TYPE, ""),
            "eta": meta.get(ALERT_CAT_ETA),
            "reason": meta.get(ALERT_CAT_REASON),
            "timestamp": event_time,
            "position": {
                "type": "Point",
                "coordinates": [payload.get(STORE_LONG), payload.get(STORE_LAT)],
            },
            "altitude": payload.get(STORE_ALT),
        }
        encoded = json.dumps(doc, separators=(",", ":"))
        try:
            pipe = self.client.pipeline()
            pipe.rpush(_KEY_ALERTS.format(flight_id=flight_id), encoded)
            pipe.lpush(_KEY_ALERTS_RECENT, encoded)
            pipe.execute()
        except RedisError as exc:
            log.error("Failed to record live alert for %s: %s", flight_id, exc)

    def get_flights(self) -> list[dict[str, Any]]:
        if not self._ensure_connected():
            return []
        assert self.client is not None
        results: list[dict[str, Any]] = []
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
            return None
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
                "zone": doc.get("zone"),
                "level": doc.get("level"),
                "start_time": doc.get("start_time"),
                "end_time": doc.get("end_time"),
                "callsign": doc.get("callsign") or info.get("callsign"),
                "model": doc.get("model") or info.get("model"),
                "owner": doc.get("owner") or info.get("owner"),
                "country": doc.get("country") or info.get("country"),
                "aircraft_type": doc.get("aircraft_type") or info.get("aircraft_type") or info.get("typecode"),
                "raw_messages": doc.get("raw_messages", []),
                "is_live": True,
                "status": "live",
            }
        except RedisError as exc:
            log.error("Failed to read live flight %s: %s", flight_id, exc)
            return None

    def get_telemetry(self, flight_id: str, *, since: float = 0.0) -> list[dict[str, Any]]:
        if not self._ensure_connected():
            return []
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
            return []
        assert self.client is not None
        points: list[dict[str, Any]] = []
        try:
            for flight_id in self.client.smembers(_KEY_FLIGHTS):
                for point in self.get_telemetry(flight_id, since=since):
                    points.append({
                        "flight_id": flight_id,
                        "icao": point.get("icao"),
                        **point,
                    })
        except RedisError as exc:
            log.error("Failed to read live telemetry: %s", exc)
        points.sort(key=lambda item: item.get("timestamp") or 0)
        return points

    def get_alerts(self, *, since: float = 0.0, flight_id: str | None = None,
                   level: str | None = None) -> list[dict[str, Any]]:
        if not self._ensure_connected():
            return []
        assert self.client is not None
        try:
            if flight_id:
                raw_alerts = self.client.lrange(_KEY_ALERTS.format(flight_id=flight_id), 0, -1)
            else:
                raw_alerts = self.client.lrange(_KEY_ALERTS_RECENT, 0, -1)
            alerts = [json.loads(raw) for raw in raw_alerts]
            if since:
                alerts = [alert for alert in alerts if alert.get("timestamp", 0) > since]
            if level:
                alerts = [alert for alert in alerts if alert.get("level") == level]
            alerts.sort(key=lambda item: item.get("timestamp") or 0, reverse=True)
            return alerts
        except RedisError as exc:
            log.error("Failed to read live alerts: %s", exc)
            return []

    def pop_flight(self, flight_id: str) -> dict[str, Any]:
        """Return buffered flight data and delete Redis keys for the flight."""
        if not self._ensure_connected():
            return {"alerts": []}
        assert self.client is not None
        try:
            raw_flight = self.client.get(_KEY_FLIGHT.format(flight_id=flight_id))
            raw_alerts = self.client.lrange(_KEY_ALERTS.format(flight_id=flight_id), 0, -1)
            pipe = self.client.pipeline()
            pipe.srem(_KEY_FLIGHTS, flight_id)
            pipe.delete(
                _KEY_FLIGHT.format(flight_id=flight_id),
                _KEY_TELEMETRY.format(flight_id=flight_id),
                _KEY_ALERTS.format(flight_id=flight_id),
            )
            pipe.execute()
            self._last_telemetry_ts.pop(flight_id, None)
            return {
                "flight": json.loads(raw_flight) if raw_flight else None,
                "alerts": [json.loads(raw) for raw in raw_alerts],
            }
        except RedisError as exc:
            log.error("Failed to pop live flight %s: %s", flight_id, exc)
            return {"alerts": []}

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def _get_last_telemetry_point(self, flight_id: str) -> dict[str, Any] | None:
        assert self.client is not None
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
            "zone": doc.get("zone"),
            "level": doc.get("level"),
            "start_time": doc.get("start_time"),
            "end_time": doc.get("end_time"),
            "callsign": doc.get("callsign") or info.get("callsign"),
            "model": doc.get("model") or info.get("model"),
            "owner": doc.get("owner") or info.get("owner"),
            "country": doc.get("country") or info.get("country"),
            "aircraft_type": doc.get("aircraft_type") or info.get("aircraft_type") or info.get("typecode"),
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
        assert self.client is not None
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
            "zone": plane.get("zone") or "",
            "level": plane.get("level") or "",
            "start_time": first_packet,
            "end_time": last_packet,
            "callsign": info.get(STORE_CALLSIGN) or "",
            "model": info.get("model") or "",
            "owner": info.get("owner") or "",
            "country": info.get("country") or "",
            "aircraft_type": info.get("aircraft_type") or info.get("typecode") or "",
            "typecode": info.get("typecode") or "",
            "info": {str(k): v for k, v in info.items()},
            "raw_messages": plane.get("raw_messages", []),
        }
        encoded = json.dumps(flight_doc, separators=(",", ":"))
        try:
            pipe = self.client.pipeline()
            pipe.sadd(_KEY_FLIGHTS, flight_id)
            pipe.set(_KEY_FLIGHT.format(flight_id=flight_id), encoded)
            pipe.execute()
            self._write_telemetry_points(plane, flight_id, icao)
        except RedisError as exc:
            log.error("Failed to upsert live flight %s: %s", flight_id, exc)

    def _write_telemetry_points(self, plane: dict, flight_id: str, icao: str) -> None:
        assert self.client is not None
        last_written = self._last_telemetry_ts.get(flight_id, 0.0)
        lat_series = plane.get(STORE_RECV_DATA, {}).get(STORE_LAT, [])
        if not lat_series:
            return

        key = _KEY_TELEMETRY.format(flight_id=flight_id)
        pipe = self.client.pipeline()
        wrote = False
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
            pipe.zadd(key, {json.dumps(point, separators=(",", ":")): lat_datum.time})
            last_written = max(last_written, lat_datum.time)
            wrote = True

        if not wrote:
            return
        try:
            pipe.execute()
            self._last_telemetry_ts[flight_id] = last_written
        except RedisError as exc:
            log.error("Failed to write live telemetry for %s: %s", flight_id, exc)
