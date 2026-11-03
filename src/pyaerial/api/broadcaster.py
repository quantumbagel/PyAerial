"""Live WebSocket broadcaster for the web portal."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from pyaerial.api.payloads import sanitize_for_json
from pyaerial.jsonutil import dumps as json_dumps
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
#: Bytes one client may have waiting (not counting the message being sent).
_CLIENT_QUEUE_BYTES = 8 * 1024 * 1024
#: Bytes of per-client messages (responses, telemetry) queued or being sent,
#: summed over every client. Past it the biggest backlog is disconnected.
_SERVER_QUEUE_BYTES = 256 * 1024 * 1024
#: How far behind the newest delivered point a late telemetry write may land
#: and still reach a client whose per-flight cursor has been pruned.
_TELEMETRY_LATE_SLACK = 30.0
_DEFAULT_STREAMS = frozenset(WS_STREAMS)
#: Stream messages that are simply skipped (not queued) when an outbox is full.
_DROP_WHEN_FULL = frozenset({"stats", "ping", "telemetry"})
#: Full-state snapshots: a newer one replaces any queued older copy.
_COALESCE = frozenset({"flights", "alerts", "stats"})
#: Never evicted to make room (the client is dropped instead).
_KEEP = frozenset({"response", "hello"})
#: One object shared by every client it is queued for; not in the server budget.
_SHARED = frozenset({"flights", "alerts", "stats", "ping"})


@dataclass(slots=True)
class _Queued:
    """A message serialized once, at enqueue time."""

    kind: str
    text: str
    #: Resolved True once sent, False if dropped (responses only).
    done: asyncio.Future | None = None

    @property
    def size(self) -> int:
        # jsonutil.dumps escapes non-ASCII, so characters == bytes.
        return len(self.text)

    def resolve(self, sent: bool) -> None:
        if self.done is not None and not self.done.done():
            self.done.set_result(sent)


class _Budget:
    """Server-wide byte count of per-client (unshared) queued messages."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def charge(self, item: _Queued) -> None:
        if item.kind not in _SHARED:
            self.used += item.size

    def release(self, item: _Queued) -> None:
        if item.kind not in _SHARED:
            self.used -= item.size

    def fits(self, item: _Queued) -> bool:
        return item.kind in _SHARED or self.used + item.size <= self.limit


class _Outbox:
    """Per-client send queue, bounded by message count and bytes.

    Never evicts responses (or hello). When full, a new message evicts the
    oldest evictable one; droppable stream messages are skipped instead. A
    refused :meth:`put` means the client is not reading; the caller drops it.
    The message being sent is not part of the byte cap (one per client) but
    stays in the server budget until the send completes.
    """

    def __init__(
        self,
        maxsize: int = _CLIENT_QUEUE_MAX,
        max_bytes: int = _CLIENT_QUEUE_BYTES,
        budget: _Budget | None = None,
    ) -> None:
        self.maxsize = maxsize
        self.max_bytes = max_bytes
        self.budget = budget or _Budget(_SERVER_QUEUE_BYTES)
        self.queued_bytes = 0
        self._items: deque[_Queued] = deque()
        self._sending: _Queued | None = None
        self._ready = asyncio.Event()

    def __len__(self) -> int:
        return len(self._items)

    @property
    def backlog_bytes(self) -> int:
        """Queued plus in-flight bytes (what dropping this client frees)."""
        sending = self._sending.size if self._sending is not None else 0
        return self.queued_bytes + sending

    def _remove(self, index: int) -> None:
        item = self._items[index]
        del self._items[index]
        self.queued_bytes -= item.size
        self.budget.release(item)
        item.resolve(False)

    def _full(self, item: _Queued) -> bool:
        if len(self._items) >= self.maxsize:
            return True
        # A single message larger than the cap still goes on an empty queue.
        return bool(self._items) and self.queued_bytes + item.size > self.max_bytes

    def put(self, item: _Queued) -> bool:
        if item.kind in _COALESCE:
            for index in reversed(range(len(self._items))):
                if self._items[index].kind == item.kind:
                    self._remove(index)
        while self._full(item):
            if item.kind in _DROP_WHEN_FULL:
                return False
            victim = next(
                (i for i, queued in enumerate(self._items) if queued.kind not in _KEEP),
                None,
            )
            if victim is None:
                return False
            self._remove(victim)
        self._items.append(item)
        self.queued_bytes += item.size
        self.budget.charge(item)
        self._ready.set()
        return True

    async def get(self) -> _Queued:
        while not self._items:
            self._ready.clear()
            await self._ready.wait()
        item = self._items.popleft()
        self.queued_bytes -= item.size
        self._sending = item
        return item

    def sent(self, item: _Queued) -> None:
        """The writer finished (or abandoned) sending ``item``."""
        if self._sending is item:
            self._sending = None
            self.budget.release(item)

    def clear(self) -> None:
        """Drop everything; pending responses resolve as not sent."""
        while self._items:
            self._remove(0)
        sending = self._sending
        if sending is not None:
            self.sent(sending)
            sending.resolve(False)

    def types(self) -> list[str]:
        return [item.kind for item in self._items]

    def messages(self) -> list[dict[str, Any]]:
        """Queued messages, decoded (for tests and debugging)."""
        import json

        return [json.loads(item.text) for item in self._items]


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

    def enqueue(self, message: dict[str, Any] | _Queued) -> bool:
        if isinstance(message, dict):
            message = _Queued(str(message.get("type")), json_dumps(message))
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


def _integrity_sig(flight: dict[str, Any]) -> tuple:
    """What a client shows of the verdict; detail text is left out (it churns)."""
    integrity = flight.get("integrity")
    if not isinstance(integrity, dict):
        return ()
    checks = integrity.get("checks")
    check_states = (
        tuple(
            sorted(
                (str(name), result.get("status") if isinstance(result, dict) else None)
                for name, result in checks.items()
            )
        )
        if isinstance(checks, dict)
        else ()
    )
    emitter = integrity.get("emitter")
    where: tuple = ()
    if isinstance(emitter, dict):
        # ~100 m: finer movement of a multilaterated fix is noise.
        where = tuple(
            round(value, 3) if isinstance(value, (int, float)) else None
            for value in (emitter.get("latitude"), emitter.get("longitude"))
        )
    return (
        integrity.get("status"),
        integrity.get("score"),  # None while the status is "unknown"
        check_states,
        where,
        bool(integrity.get("rebroadcast")),
    )


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
            # Re-scored every few seconds, with or without a new position.
            _integrity_sig(flight),
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
        # Held from reading live state to enqueueing it (snapshots and the
        # flights/alerts/stats push) so an older read can never be queued
        # after a newer one. The reads run in threads; the loop stays free.
        self._state_lock = asyncio.Lock()
        self._budget = _Budget(_SERVER_QUEUE_BYTES)
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @property
    def queued_bytes(self) -> int:
        """Server-wide bytes of per-client messages queued or being sent."""
        return self._budget.used

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
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
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        now = time.time()
        client = _Client(
            telemetry_floor=now, last_ping=now, outbox=_Outbox(budget=self._budget)
        )
        if streams is not None:
            chosen = [name for name in streams if name in _DEFAULT_STREAMS]
            if chosen:
                client.streams = set(chosen)
        client.enqueue(websocket_hello())
        client.writer_task = self._spawn(self._writer(websocket, client))
        async with self._state_lock:
            await self._enqueue_snapshot(client, None)
            # Registered only now: no push older than the snapshot reaches
            # it, and the next push is compared against current state.
            self._clients[websocket] = client

    async def _writer(self, websocket: WebSocket, client: _Client) -> None:
        try:
            while True:
                item = await client.outbox.get()
                delivered = False
                try:
                    await websocket.send_text(item.text)
                    delivered = True
                finally:
                    # Also on cancel/failure, so nobody waits on it forever.
                    client.outbox.sent(item)
                    item.resolve(delivered)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A failed send means the socket is gone or broken: detach it.
            log.debug("WebSocket send failed (%s); dropping client", type(exc).__name__)
        finally:
            self.disconnect(websocket)
            client.outbox.clear()

    def send(self, websocket: WebSocket, message: dict[str, Any]) -> bool:
        """Queue a small message (errors, subscribe replies). False if dropped."""
        kind = str(message.get("type"))
        return self._put(websocket, _Queued(kind, json_dumps(message))) is not None

    def send_response(self, websocket: WebSocket, text: str) -> asyncio.Future | None:
        """Queue a serialized RPC response.

        Returns a future that resolves True once the writer has sent it (False
        if the client went away first), or None when it was not queued. The
        caller holds the request's in-flight slot until then, so a client that
        does not read cannot pile up responses.
        """
        loop = asyncio.get_running_loop()
        return self._put(websocket, _Queued("response", text, loop.create_future()))

    def _put(self, websocket: WebSocket, item: _Queued) -> asyncio.Future | bool | None:
        client = self._clients.get(websocket)
        if client is None:
            item.resolve(False)
            return None
        if not self._make_room(websocket, item):
            return None
        if client.outbox.put(item):
            return item.done if item.done is not None else True
        item.resolve(False)
        if item.kind == "response":
            log.warning("WebSocket client is not reading responses; disconnecting it")
            self._drop(websocket, "client too slow")
        return None

    def _make_room(self, websocket: WebSocket, item: _Queued) -> bool:
        """Keep the server-wide budget: drop the biggest backlogs if needed."""
        while not self._budget.fits(item):
            if item.kind in _DROP_WHEN_FULL or not self._clients:
                item.resolve(False)
                return False
            victim = max(
                self._clients, key=lambda ws: self._clients[ws].outbox.backlog_bytes
            )
            log.warning(
                "WebSocket send queues hold %d bytes (budget %d); dropping the "
                "client with the largest backlog",
                self._budget.used,
                self._budget.limit,
            )
            self._drop(victim, "server send budget exceeded")
            if victim is websocket:
                item.resolve(False)
                return False
        return True

    def _drop(self, websocket: WebSocket, reason: str) -> None:
        client = self._clients.get(websocket)
        self.disconnect(websocket)
        if client is not None:
            client.outbox.clear()
        self._spawn(_close_quietly(websocket, 1013, reason))

    def disconnect(self, websocket: WebSocket) -> None:
        client = self._clients.pop(websocket, None)
        if client is None:
            return
        task = client.writer_task
        client.writer_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        else:
            client.outbox.clear()

    def set_streams(self, websocket: WebSocket, streams: Any) -> list[str]:
        client = self._clients.get(websocket)
        if client is None:
            return []
        before = set(client.streams)
        selected = self._select_streams(client, streams)
        added = set(selected) - before
        if "telemetry" in added:
            # New points from now on; past ones come from fetchTelemetry. An
            # old floor would replay (and re-read) the whole Redis backlog.
            client.telemetry_floor = time.time()
            client.telemetry_marks.clear()
        # Pushes only follow changes, so a newly added stream needs its
        # current state now or the client waits for the next change.
        added -= {"telemetry"}
        if added:
            self._spawn(self._added_streams_snapshot(websocket, added))
        return selected

    async def _added_streams_snapshot(self, websocket: WebSocket, added: set[str]) -> None:
        try:
            await self._send_snapshot(websocket, only=added)
        except Exception:
            log.exception("Snapshot after subscribe failed")

    @staticmethod
    def _select_streams(client: _Client, streams: Any) -> list[str]:
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

    async def _send_snapshot(
        self, websocket: WebSocket, *, only: set[str] | None = None
    ) -> None:
        async with self._state_lock:
            client = self._clients.get(websocket)
            if client is not None:
                await self._enqueue_snapshot(client, only)

    async def _enqueue_snapshot(self, client: _Client, only: set[str] | None) -> None:
        """Read current state and queue it. Caller holds ``_state_lock``."""
        stats = await self._stats()
        live_ok = True
        flights: list[dict[str, Any]] = []
        alerts: list[dict[str, Any]] = []
        if only is None or only & {"flights", "alerts"}:
            try:
                flights, alerts = await asyncio.to_thread(self._live_snapshot)
            except LiveUnavailable:
                live_ok = False
        streams = client.streams if only is None else client.streams & only
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
            # Outside the data tick: clients drop a socket after missed pings,
            # and a failing Redis read must not look like a dead connection.
            self._send_pings(time.time())
            await asyncio.sleep(_LIVE_POLL_INTERVAL)

    def _send_pings(self, now: float) -> None:
        due = [c for c in self._clients.values() if now - c.last_ping >= _PING_INTERVAL]
        if not due:
            return
        ping = _Queued("ping", json_dumps({"type": "ping", "timestamp": now}))
        for client in due:
            client.outbox.put(ping)
            client.last_ping = now

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
        async with self._state_lock:
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
                self._store_stats(data.stats)
                if data.stats is not None
                else cached_stats
            )

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
        now = data.now

        for websocket, client in list(self._clients.items()):
            if data.points and "telemetry" in client.streams:
                points = client.select_telemetry(data.points)
                if points:
                    self.send(
                        websocket,
                        {"type": "telemetry", "telemetry": points, "timestamp": now},
                    )
                    # Advance even when the full outbox dropped the batch: a
                    # stalled client otherwise pins telemetry_since, and every
                    # tick re-reads (and re-offers) the whole Redis backlog.
                    client.mark_telemetry_sent(points)

    def _broadcast(self, message: dict[str, Any]) -> None:
        stream = str(message.get("type"))
        targets = [c for c in self._clients.values() if stream in c.streams]
        if not targets:
            return
        item = _Queued(stream, json_dumps(message))  # serialized once, shared
        for client in targets:
            client.outbox.put(item)


async def _close_quietly(websocket: WebSocket, code: int, reason: str) -> None:
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    try:
        await websocket.close(code=code, reason=reason)
    except (RuntimeError, OSError, WebSocketDisconnect) as exc:
        # The peer is already gone; that is what we wanted.
        log.debug("WebSocket close failed: %s", type(exc).__name__)
