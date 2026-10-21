"""Live WebSocket broadcaster for the web portal."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from pyaerial.api.payloads import sanitize_for_json
from pyaerial.api.queries import (
    engine_is_live,
    get_live_flights,
    get_stats,
    get_tracked_live_alerts,
)
from pyaerial.api.spec import ALERTS_PUSH_MIN, WS_STREAMS, websocket_hello
from pyaerial.enrich.aircraft_db import AircraftDB
from pyaerial.store.live import LiveStore
from pyaerial.store.redis_live import LiveUnavailable
import contextlib

log = logging.getLogger("pyaerial.webapp")

_LIVE_POLL_INTERVAL = 1.0
_PING_INTERVAL = 15.0
_STATS_CACHE_TTL = 5.0
_CLIENT_QUEUE_MAX = 64
#: How far behind the newest delivered point a late telemetry write may land
#: and still reach a client whose per-flight cursor has been pruned.
_TELEMETRY_LATE_SLACK = 30.0
_DEFAULT_STREAMS = frozenset(WS_STREAMS)
#: Stream messages that are simply skipped (not queued) when an outbox is full.
_DROP_WHEN_FULL = frozenset({"stats", "ping", "telemetry"})


class _Outbox:
    """Bounded per-client send queue that never evicts ``response`` messages.

    When full, a new stream snapshot evicts the oldest non-response message.
    If every queued message is a response the client is not reading at all;
    :meth:`put` then refuses the message and the caller drops the client.
    """

    def __init__(self, maxsize: int = _CLIENT_QUEUE_MAX) -> None:
        self.maxsize = maxsize
        self._items: deque[dict[str, Any]] = deque()
        self._ready = asyncio.Event()

    def __len__(self) -> int:
        return len(self._items)

    def put(self, message: dict[str, Any]) -> bool:
        if len(self._items) >= self.maxsize:
            if message.get("type") in _DROP_WHEN_FULL:
                return False
            for index, queued in enumerate(self._items):
                if queued.get("type") != "response":
                    del self._items[index]
                    break
            else:
                return False
        self._items.append(message)
        self._ready.set()
        return True

    async def get(self) -> dict[str, Any]:
        while not self._items:
            self._ready.clear()
            await self._ready.wait()
        return self._items.popleft()

    def types(self) -> list[Any]:
        return [item.get("type") for item in self._items]


@dataclass
class _Client:
    #: Points at or before this time are never sent (connect time, then
    #: advanced to newest-delivered minus the late-write slack).
    telemetry_floor: float
    last_ping: float
    streams: set[str] = field(default_factory=lambda: set(_DEFAULT_STREAMS))
    outbox: _Outbox = field(default_factory=_Outbox)
    writer_task: asyncio.Task | None = None
    #: Newest delivered telemetry timestamp per flight_id.
    telemetry_marks: dict[str, float] = field(default_factory=dict)

    def enqueue(self, message: dict[str, Any]) -> bool:
        return self.outbox.put(message)

    def select_telemetry(self, points: list[dict[str, Any]]) -> list[dict[str, Any]]:
        selected = []
        for point in points:
            ts = _timestamp(point)
            mark = self.telemetry_marks.get(str(point.get("flight_id")), self.telemetry_floor)
            if ts > mark:
                selected.append(point)
        return selected

    def mark_telemetry_sent(self, points: list[dict[str, Any]]) -> None:
        newest = self.telemetry_floor
        for point in points:
            ts = _timestamp(point)
            key = str(point.get("flight_id"))
            if ts > self.telemetry_marks.get(key, self.telemetry_floor):
                self.telemetry_marks[key] = ts
            newest = max(newest, ts)
        floor = max(self.telemetry_floor, newest - _TELEMETRY_LATE_SLACK)
        self.telemetry_floor = floor
        # Cursors at or below the floor add nothing over the floor itself.
        for key in [k for k, v in self.telemetry_marks.items() if v <= floor]:
            del self.telemetry_marks[key]


def _timestamp(point: dict[str, Any]) -> float:
    try:
        return float(point.get("timestamp") or 0)
    except (TypeError, ValueError):
        return 0.0


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
    return tuple(
        sorted(
            (key, stats[key])
            for key in stats
            if isinstance(stats[key], (int, float, str, bool))
        )
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


@dataclass
class _TickData:
    flights: list[dict[str, Any]] | None
    alerts: list[dict[str, Any]] | None
    stats: dict[str, Any] | None
    points: list[dict[str, Any]]
    now: float


class LiveBroadcaster:
    """Poll the live store and push updates to connected WebSocket clients.

    Client bookkeeping (``_clients``, telemetry cursors, the stats cache) is
    only touched on the event loop; worker threads receive plain values and
    return plain values.
    """

    def __init__(
        self,
        live_store: LiveStore | None,
        aircraft_db: AircraftDB | None,
        history: Any | None = None,
    ):
        self.live_store = live_store
        self.aircraft_db = aircraft_db
        self.history = history
        self._clients: dict[WebSocket, _Client] = {}
        self._task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._pending_lookups: set[str] = set()
        self._last_flights_sig: tuple | None = None
        self._last_alerts_sig: tuple | None = None
        self._last_stats_sig: tuple | None = None
        self._last_stats: dict[str, Any] | None = None
        self._last_stats_at = 0.0

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        for websocket in list(self._clients):
            self.disconnect(websocket)
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def connect(
        self,
        websocket: WebSocket,
        *,
        streams: list[str] | None = None,
    ) -> None:
        await websocket.accept()
        now = time.time()
        client = _Client(telemetry_floor=now, last_ping=now)
        if streams is not None:
            chosen = [name for name in streams if name in _DEFAULT_STREAMS]
            if chosen:
                client.streams = set(chosen)
        client.enqueue(websocket_hello())
        self._clients[websocket] = client
        client.writer_task = self._spawn(self._writer(websocket, client))
        await self._send_snapshot(websocket)

    async def _writer(self, websocket: WebSocket, client: _Client) -> None:
        try:
            while True:
                message = await client.outbox.get()
                await websocket.send_json(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A failed send means the socket is gone or broken: detach it.
            log.debug("WebSocket send failed (%s); dropping client", type(exc).__name__)
        finally:
            self.disconnect(websocket)

    def send(self, websocket: WebSocket, message: dict[str, Any]) -> bool:
        client = self._clients.get(websocket)
        if client is None:
            return False
        if client.enqueue(message):
            return True
        if message.get("type") == "response":
            log.warning("WebSocket client is not reading responses; disconnecting it")
            self.disconnect(websocket)
            self._spawn(_close_quietly(websocket, 1013, "client too slow"))
        return False

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
        if not streams:
            client.streams = set(_DEFAULT_STREAMS)
        else:
            if isinstance(streams, str):
                streams = [streams]
            if not isinstance(streams, (list, tuple)):
                return []
            chosen = {str(name) for name in streams if str(name) in _DEFAULT_STREAMS}
            if not chosen:
                return []
            client.streams = chosen
        return sorted(client.streams)

    # -- stats cache (event loop only) ---------------------------------------

    def _fresh_cached_stats(self) -> dict[str, Any] | None:
        if (
            self._last_stats is not None
            and time.monotonic() - self._last_stats_at < _STATS_CACHE_TTL
        ):
            return self._last_stats
        return None

    def _store_stats(self, stats: dict[str, Any]) -> dict[str, Any]:
        self._last_stats = stats
        self._last_stats_at = time.monotonic()
        return stats

    async def _stats(self) -> dict[str, Any]:
        cached = self._fresh_cached_stats()
        if cached is not None:
            return cached
        stats = await asyncio.to_thread(get_stats, self.live_store, self.history)
        return self._store_stats(stats)

    # -- snapshot -------------------------------------------------------------

    def _live_snapshot(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Runs in a worker thread; touches no broadcaster state."""
        if not self.live_store:
            return [], []
        flights = get_live_flights(self.live_store, self.aircraft_db)
        alerts = get_tracked_live_alerts(
            self.live_store, flights, limit=ALERTS_PUSH_MIN
        )
        return sanitize_for_json(flights), sanitize_for_json(alerts)

    async def _send_snapshot(self, websocket: WebSocket) -> None:
        stats = await self._stats()
        live_ok = True
        flights: list[dict[str, Any]] = []
        alerts: list[dict[str, Any]] = []
        try:
            flights, alerts = await asyncio.to_thread(self._live_snapshot)
        except LiveUnavailable:
            live_ok = False
        client = self._clients.get(websocket)
        if client is None:
            return
        streams = client.streams
        if live_ok and "flights" in streams:
            client.enqueue({"type": "flights", "flights": flights})
        if live_ok and "alerts" in streams:
            client.enqueue({"type": "alerts", "alerts": alerts})
        if "stats" in streams:
            client.enqueue({"type": "stats", "stats": sanitize_for_json(stats)})

    # -- polling loop ---------------------------------------------------------

    async def _run_loop(self) -> None:
        while True:
            try:
                await self._background_tick()
                if self._clients:
                    await self._broadcast_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Live broadcaster tick failed")
            await asyncio.sleep(_LIVE_POLL_INTERVAL)

    async def _background_tick(self) -> None:
        if not self._clients:
            return
        if not self.live_store:
            return
        try:
            flights = await asyncio.to_thread(self.live_store.get_flights)
        except LiveUnavailable:
            return

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
                    self._spawn(self._bg_fetch_aircraft(icao_clean))

    async def _bg_fetch_aircraft(self, icao: str) -> None:
        try:
            if self.aircraft_db:
                await asyncio.to_thread(self.aircraft_db.lookup_cached, icao)
        except Exception as exc:
            log.warning("Background aircraft DB lookup failed for %s: %s", icao, exc)
        finally:
            self._pending_lookups.discard(icao)

    def _collect_live_payload(
        self, telemetry_since: float | None, refresh_stats: bool
    ) -> _TickData:
        """Runs in a worker thread.

        Receives everything it needs as arguments (no ``_clients`` access) and
        returns fresh values for the event loop to apply.
        """
        now = time.time()
        stats = get_stats(self.live_store, self.history) if refresh_stats else None
        if not self.live_store:
            return _TickData([], [], stats, [], now)
        try:
            flights = get_live_flights(self.live_store, self.aircraft_db)
            alerts = get_tracked_live_alerts(
                self.live_store, flights, limit=ALERTS_PUSH_MIN
            )
        except LiveUnavailable:
            return _TickData(None, None, stats, [], now)
        points: list[dict[str, Any]] = []
        if telemetry_since is not None and engine_is_live(self.live_store):
            try:
                points = self.live_store.get_live_telemetry(telemetry_since)
            except LiveUnavailable:
                points = []
        return _TickData(
            sanitize_for_json(flights),
            sanitize_for_json(alerts),
            stats,
            sanitize_for_json(points),
            now,
        )

    async def _broadcast_tick(self) -> None:
        if not self._clients:
            return
        floors = [
            client.telemetry_floor
            for client in self._clients.values()
            if "telemetry" in client.streams
        ]
        telemetry_since = min(floors) if floors else None
        cached_stats = self._fresh_cached_stats()
        data = await asyncio.to_thread(
            self._collect_live_payload, telemetry_since, cached_stats is None
        )
        stats = (
            self._store_stats(data.stats) if data.stats is not None else cached_stats
        )
        now = data.now

        if data.flights is not None and data.alerts is not None:
            flights_sig = _flights_sig(data.flights)
            if flights_sig != self._last_flights_sig:
                self._last_flights_sig = flights_sig
                self._broadcast({"type": "flights", "flights": data.flights})

            alerts_sig = _alerts_sig(data.alerts)
            if alerts_sig != self._last_alerts_sig:
                self._last_alerts_sig = alerts_sig
                self._broadcast({"type": "alerts", "alerts": data.alerts})

        stats_sig = _stats_sig(stats)
        if stats is not None and stats_sig != self._last_stats_sig:
            self._last_stats_sig = stats_sig
            self._broadcast({"type": "stats", "stats": sanitize_for_json(stats)})

        for client in list(self._clients.values()):
            if data.points and "telemetry" in client.streams:
                points = client.select_telemetry(data.points)
                if points:
                    queued = client.enqueue(
                        {"type": "telemetry", "telemetry": points, "timestamp": now}
                    )
                    if queued:
                        client.mark_telemetry_sent(points)
            if now - client.last_ping >= _PING_INTERVAL:
                client.enqueue({"type": "ping", "timestamp": now})
                client.last_ping = now

    def _broadcast(self, message: dict[str, Any]) -> None:
        stream = message.get("type")
        for client in list(self._clients.values()):
            if stream not in client.streams:
                continue
            client.enqueue(message)


async def _close_quietly(websocket: WebSocket, code: int, reason: str) -> None:
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    try:
        await websocket.close(code=code, reason=reason)
    except (RuntimeError, OSError) as exc:
        log.debug("WebSocket close failed: %s", type(exc).__name__)
