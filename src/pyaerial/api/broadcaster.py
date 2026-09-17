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
from pyaerial.api.spec import WS_STREAMS, websocket_hello, websocket_raw_hello
from pyaerial.enrich.aircraft_db import AircraftDB

log = logging.getLogger("pyaerial.webapp")

_LIVE_POLL_INTERVAL = 1.0
_PING_INTERVAL = 15.0
_STATS_CACHE_TTL = 5.0
_RAW_QUEUE_MAX = 64
_CLIENT_QUEUE_MAX = 64
_DEFAULT_STREAMS = frozenset(WS_STREAMS)
_RAW_STREAMS = frozenset({"raw"})


@dataclass
class _Client:
    telemetry_since: float
    last_ping: float
    streams: set[str] = field(default_factory=lambda: set(_DEFAULT_STREAMS))
    raw_only: bool = False
    outbox: asyncio.Queue | None = None
    writer_task: asyncio.Task | None = None

    def enqueue(self, message: dict[str, Any]) -> None:
        queue = self.outbox
        if queue is None:
            return
        if queue.full():
            if message.get("type") in {"stats", "ping"}:
                return
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            pass


def _flights_sig(flights: list[dict[str, Any]]) -> tuple:
    return tuple(
        (
            flight.get("flight_id"),
            flight.get("timestamp"),
            flight.get("latitude"),
            flight.get("longitude"),
            flight.get("callsign"),
            flight.get("model"),
            flight.get("owner"),
            len(flight.get("active_alerts") or []),
        )
        for flight in flights
    )


def _stats_sig(stats: dict[str, Any] | None) -> tuple:
    if not stats:
        return ()
    return tuple(sorted((key, stats[key]) for key in stats if isinstance(stats[key], (int, float, str, bool))))


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
        history: Any | None = None,
        antenna: dict[str, Any] | None = None,
    ):
        self.live_store = live_store
        self.aircraft_db = aircraft_db
        self.history = history
        self.antenna = antenna or {}
        self._clients: dict[WebSocket, _Client] = {}
        self._task: asyncio.Task | None = None
        self._raw_task: asyncio.Task | None = None
        self._raw_queue: asyncio.Queue | None = None
        self._pending_lookups: set[str] = set()
        self._last_flights_sig: tuple | None = None
        self._last_alerts_sig: tuple | None = None
        self._last_stats_sig: tuple | None = None
        self._last_stats: dict[str, Any] | None = None
        self._last_stats_at = 0.0

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop())
        store = self.live_store
        start_pubsub = getattr(store, "start_raw_pubsub", None)
        if not callable(start_pubsub):
            return
        self._raw_queue = asyncio.Queue(maxsize=_RAW_QUEUE_MAX)
        loop = asyncio.get_running_loop()
        start_pubsub(lambda payload: self._enqueue_raw(loop, payload))
        self._raw_task = asyncio.create_task(self._raw_loop())

    async def stop(self) -> None:
        store = self.live_store
        stop_pubsub = getattr(store, "stop_raw_pubsub", None)
        if callable(stop_pubsub):
            stop_pubsub()
        for task in (self._raw_task, self._task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._raw_task = None
        self._task = None

    def _enqueue_raw(self, loop: asyncio.AbstractEventLoop, payload: dict[str, Any]) -> None:
        queue = self._raw_queue
        if queue is None:
            return

        def _put() -> None:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:
            pass

    async def _raw_loop(self) -> None:
        queue = self._raw_queue
        if queue is None:
            return
        while True:
            payload = await queue.get()
            try:
                await self._broadcast_raw(payload)
            except Exception:
                log.exception("Raw sensor broadcast failed")

    async def _broadcast_raw(self, payload: dict[str, Any]) -> None:
        messages = payload.get("messages") or []
        if not messages:
            return
        now = payload.get("timestamp") or time.time()
        await self._broadcast(
            {
                "type": "raw",
                "timestamp": now,
                "messages": sanitize_for_json(messages),
            }
        )

    async def connect(
        self,
        websocket: WebSocket,
        *,
        streams: list[str] | None = None,
        raw_only: bool = False,
    ) -> None:
        await websocket.accept()
        now = time.time()
        client = _Client(telemetry_since=now, last_ping=now, raw_only=raw_only)
        client.outbox = asyncio.Queue(maxsize=_CLIENT_QUEUE_MAX)
        self._clients[websocket] = client
        client.writer_task = asyncio.create_task(self._writer(websocket, client))
        if raw_only:
            client.streams = set(_RAW_STREAMS)
            client.enqueue(websocket_raw_hello())
            client.enqueue(
                {
                    "type": "antenna",
                    "timestamp": now,
                    "antenna": sanitize_for_json(self.antenna),
                }
            )
            return
        if streams is not None:
            self.set_streams(websocket, streams)
        client.enqueue(websocket_hello())
        await self._send_snapshot(websocket)

    async def _writer(self, websocket: WebSocket, client: _Client) -> None:
        queue = client.outbox
        if queue is None:
            return
        try:
            while True:
                message = await queue.get()
                if message is None:
                    return
                await websocket.send_json(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self.disconnect(websocket)

    def send(self, websocket: WebSocket, message: dict[str, Any]) -> None:
        client = self._clients.get(websocket)
        if client is not None:
            client.enqueue(message)

    def disconnect(self, websocket: WebSocket) -> None:
        client = self._clients.pop(websocket, None)
        if client is None:
            return
        task = client.writer_task
        client.writer_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def set_streams(self, websocket: WebSocket, streams: Any) -> list[str]:
        client = self._clients.get(websocket)
        if client is None:
            return []
        if client.raw_only:
            return ["raw"]
        if not streams:
            client.streams = set(_DEFAULT_STREAMS)
        else:
            if isinstance(streams, str):
                streams = [streams]
            chosen = {str(name) for name in streams if str(name) in _DEFAULT_STREAMS}
            if not chosen:
                return []
            client.streams = chosen
        return sorted(client.streams)

    def _cached_stats(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._last_stats is not None and now - self._last_stats_at < _STATS_CACHE_TTL:
            return self._last_stats
        stats = get_stats(self.live_store, self.history)
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
        streams = client.streams if client else _DEFAULT_STREAMS
        if client is None:
            return
        if "flights" in streams:
            client.enqueue(
                {"type": "flights", "flights": sanitize_for_json(flights)}
            )
        if "alerts" in streams:
            client.enqueue(
                {"type": "alerts", "alerts": sanitize_for_json(alerts)}
            )
        if "stats" in streams:
            client.enqueue(
                {"type": "stats", "stats": sanitize_for_json(stats)}
            )

    async def send_antenna(self, websocket: WebSocket) -> None:
        self.send(
            websocket,
            {
                "type": "antenna",
                "timestamp": time.time(),
                "antenna": sanitize_for_json(self.antenna),
            },
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
        if not self._clients or all(c.raw_only for c in self._clients.values()):
            return
        if not self.live_store:
            return
        flights = await asyncio.to_thread(self.live_store.get_flights)

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

    def _collect_live_payload(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]], float]:
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
        telemetry_clients = [
            client
            for client in self._clients.values()
            if "telemetry" in client.streams
        ]
        all_points: list[dict[str, Any]] = []
        if telemetry_clients and self.live_store:
            min_since = min(client.telemetry_since for client in telemetry_clients)
            all_points = self.live_store.get_live_telemetry(min_since)
        return flights, alerts, stats, all_points, now

    async def _broadcast_tick(self) -> None:
        clients = list(self._clients.values())
        if not clients:
            return
        now = time.time()
        if all(client.raw_only for client in clients):
            for client in clients:
                if now - client.last_ping >= _PING_INTERVAL:
                    client.enqueue({"type": "ping", "timestamp": now})
                    client.last_ping = now
            return

        flights, alerts, stats, all_points, now = await asyncio.to_thread(
            self._collect_live_payload
        )

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

        stats_sig = _stats_sig(stats)
        if stats_sig != self._last_stats_sig:
            self._last_stats_sig = stats_sig
            await self._broadcast({"type": "stats", "stats": sanitize_for_json(stats)})

        for _websocket, client in list(self._clients.items()):
            if "telemetry" in client.streams:
                points = [
                    point
                    for point in all_points
                    if point.get("timestamp", 0) > client.telemetry_since
                ]
                if points:
                    client.enqueue(
                        {
                            "type": "telemetry",
                            "telemetry": sanitize_for_json(points),
                            "timestamp": now,
                        }
                    )
                    max_ts = max(float(point.get("timestamp") or 0) for point in points)
                    client.telemetry_since = max(max_ts, client.telemetry_since)
            if now - client.last_ping >= _PING_INTERVAL:
                client.enqueue({"type": "ping", "timestamp": now})
                client.last_ping = now

    async def _broadcast(self, message: dict[str, Any]) -> None:
        stream = message.get("type")
        for _websocket, client in list(self._clients.items()):
            if stream not in client.streams:
                continue
            client.enqueue(message)
