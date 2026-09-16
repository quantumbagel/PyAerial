"""Live WebSocket broadcaster for the web portal."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from pyaerial.api.payloads import sanitize_for_json
from pyaerial.api.protocol import LiveStore
from pyaerial.api.queries import get_live_flights, get_stats, get_tracked_live_alerts
from pyaerial.api.spec import WS_STREAMS, websocket_hello
from pyaerial.enrich.aircraft_db import AircraftDB

log = logging.getLogger("pyaerial.webapp")

_LIVE_POLL_INTERVAL = 1.0
_PING_INTERVAL = 15.0
_STATS_CACHE_TTL = 5.0
_ALL_STREAMS = frozenset(WS_STREAMS)


@dataclass
class _Client:
    telemetry_since: float
    last_ping: float
    streams: set[str] = field(default_factory=lambda: set(WS_STREAMS))


def _flights_sig(flights: list[dict[str, Any]]) -> tuple:
    return tuple(
        (
            flight.get("flight_id"),
            flight.get("timestamp"),
            flight.get("latitude"),
            flight.get("longitude"),
            len(flight.get("active_alerts") or []),
        )
        for flight in flights
    )


def _alerts_sig(alerts: list[dict[str, Any]]) -> tuple:
    return tuple(
        (
            alert.get("alert_id"),
            alert.get("active"),
            alert.get("eta"),
            alert.get("deactivated_at"),
        )
        for alert in alerts
    )


class LiveBroadcaster:
    """Poll the live store and push updates to connected WebSocket clients."""

    def __init__(
        self,
        live_store: LiveStore | None,
        aircraft_db: AircraftDB | None,
        db: Any | None = None,
    ):
        self.live_store = live_store
        self.aircraft_db = aircraft_db
        self.db = db
        self._clients: dict[WebSocket, _Client] = {}
        self._task: asyncio.Task | None = None
        self._pending_lookups: set[str] = set()
        self._last_flights_sig: tuple | None = None
        self._last_alerts_sig: tuple | None = None
        self._last_stats: dict[str, Any] | None = None
        self._last_stats_at = 0.0

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        now = time.time()
        self._clients[websocket] = _Client(telemetry_since=now, last_ping=now)
        await websocket.send_json(websocket_hello())
        await self._send_snapshot(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.pop(websocket, None)

    def set_streams(self, websocket: WebSocket, streams: Any) -> list[str]:
        client = self._clients.get(websocket)
        if client is None:
            return []
        if not streams:
            client.streams = set(WS_STREAMS)
        else:
            if isinstance(streams, str):
                streams = [streams]
            chosen = {str(name) for name in streams if str(name) in _ALL_STREAMS}
            client.streams = chosen or set(WS_STREAMS)
        return sorted(client.streams)

    def _cached_stats(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._last_stats is not None and now - self._last_stats_at < _STATS_CACHE_TTL:
            return self._last_stats
        stats = get_stats(self.live_store, self.db)
        self._last_stats = stats
        self._last_stats_at = now
        return stats

    async def _send_snapshot(self, websocket: WebSocket) -> None:
        flights = (
            get_live_flights(self.live_store, self.aircraft_db)
            if self.live_store
            else []
        )
        alerts = (
            get_tracked_live_alerts(self.live_store, flights, limit=50)
            if self.live_store
            else []
        )
        stats = self._cached_stats()
        client = self._clients.get(websocket)
        streams = client.streams if client else _ALL_STREAMS
        if "flights" in streams:
            await websocket.send_json(
                {"type": "flights", "flights": sanitize_for_json(flights)}
            )
        if "alerts" in streams:
            await websocket.send_json(
                {"type": "alerts", "alerts": sanitize_for_json(alerts)}
            )
        if "stats" in streams:
            await websocket.send_json(
                {"type": "stats", "stats": sanitize_for_json(stats)}
            )

    async def _run_loop(self) -> None:
        while True:
            try:
                await self._background_tick()
                if self._clients:
                    await self._broadcast_tick()
            except Exception:
                log.exception("Live broadcaster tick failed")
            await asyncio.sleep(_LIVE_POLL_INTERVAL)

    async def _background_tick(self) -> None:
        flights = self.live_store.get_flights() if self.live_store else []

        if self.aircraft_db and self.aircraft_db.available and flights:
            for flight in flights:
                icao = flight.get("icao")
                if not icao:
                    continue
                icao_clean = str(icao).lower().strip()
                if not icao_clean or icao_clean in self._pending_lookups:
                    continue

                if not self.aircraft_db.is_cached(icao_clean):
                    self._pending_lookups.add(icao_clean)
                    asyncio.create_task(self._bg_fetch_aircraft(icao_clean))

    async def _bg_fetch_aircraft(self, icao: str) -> None:
        try:
            if self.aircraft_db:
                await asyncio.to_thread(self.aircraft_db.lookup_cached, icao)
        except Exception as exc:
            log.warning("Background aircraft DB lookup failed for %s: %s", icao, exc)
        finally:
            self._pending_lookups.discard(icao)

    async def _broadcast_tick(self) -> None:
        now = time.time()

        flights = (
            get_live_flights(self.live_store, self.aircraft_db)
            if self.live_store
            else []
        )
        alerts = (
            get_tracked_live_alerts(self.live_store, flights, limit=50)
            if self.live_store
            else []
        )
        stats = self._cached_stats()

        flights_sig = _flights_sig(flights)
        if flights_sig != self._last_flights_sig:
            self._last_flights_sig = flights_sig
            await self._broadcast(
                {"type": "flights", "flights": sanitize_for_json(flights)}
            )

        alerts_sig = _alerts_sig(alerts)
        if alerts_sig != self._last_alerts_sig:
            self._last_alerts_sig = alerts_sig
            await self._broadcast(
                {"type": "alerts", "alerts": sanitize_for_json(alerts)}
            )

        await self._broadcast({"type": "stats", "stats": sanitize_for_json(stats)})

        min_since = (
            min(client.telemetry_since for client in self._clients.values())
            if self._clients
            else now
        )
        all_points = (
            self.live_store.get_live_telemetry(min_since) if self.live_store else []
        )
        for websocket, client in list(self._clients.items()):
            if "telemetry" in client.streams:
                points = [
                    point
                    for point in all_points
                    if point.get("timestamp", 0) > client.telemetry_since
                ]
                if points:
                    payload = {
                        "type": "telemetry",
                        "telemetry": sanitize_for_json(points),
                        "timestamp": now,
                    }
                    try:
                        await websocket.send_json(payload)
                        client.telemetry_since = now
                    except Exception:
                        self.disconnect(websocket)
                        continue
            if now - client.last_ping >= _PING_INTERVAL:
                try:
                    await websocket.send_json({"type": "ping", "timestamp": now})
                    client.last_ping = now
                except Exception:
                    self.disconnect(websocket)

    async def _broadcast(self, message: dict[str, Any]) -> None:
        stream = message.get("type")
        for websocket, client in list(self._clients.items()):
            if stream not in client.streams:
                continue
            try:
                await websocket.send_json(message)
            except Exception:
                self.disconnect(websocket)
