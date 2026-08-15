"""Live WebSocket broadcaster for the web portal."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import WebSocket

from pyaerial.api.payloads import sanitize_for_json
from pyaerial.api.protocol import LiveStore
from pyaerial.api.queries import get_live_flights, get_stats, get_tracked_live_alerts
from pyaerial.calc.aircraft_db import AircraftDB

log = logging.getLogger("pyaerial.webapp")

_LIVE_POLL_INTERVAL = 1.0


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
        self._clients: dict[WebSocket, float] = {}
        self._task: asyncio.Task | None = None
        self._pending_lookups: set[str] = set()
        self._last_flights_sig: str | None = None
        self._last_alerts_sig: str | None = None
        self._last_stats_sig: str | None = None

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
        self._clients[websocket] = 0.0
        await self._send_snapshot(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.pop(websocket, None)

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
        stats = get_stats(self.live_store, self.db, self.aircraft_db)
        await websocket.send_json(
            {"type": "flights", "flights": sanitize_for_json(flights)}
        )
        await websocket.send_json(
            {"type": "alerts", "alerts": sanitize_for_json(alerts)}
        )
        await websocket.send_json({"type": "stats", "stats": sanitize_for_json(stats)})

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
        stats = get_stats(self.live_store, self.db, self.aircraft_db)

        flights_sig = json.dumps(
            sanitize_for_json(flights), sort_keys=True, default=str
        )
        if flights_sig != self._last_flights_sig:
            self._last_flights_sig = flights_sig
            msg = {"type": "flights", "flights": sanitize_for_json(flights)}
            await self._broadcast(msg)

        alerts_sig = json.dumps(sanitize_for_json(alerts), sort_keys=True, default=str)
        if alerts_sig != self._last_alerts_sig:
            self._last_alerts_sig = alerts_sig
            msg = {"type": "alerts", "alerts": sanitize_for_json(alerts)}
            await self._broadcast(msg)

        stats_sig = json.dumps(sanitize_for_json(stats), sort_keys=True, default=str)
        if stats_sig != self._last_stats_sig:
            self._last_stats_sig = stats_sig
            msg = {"type": "stats", "stats": sanitize_for_json(stats)}
            await self._broadcast(msg)

        for websocket, since in list(self._clients.items()):
            points = (
                self.live_store.get_live_telemetry(since) if self.live_store else []
            )

            if points:
                payload = {
                    "type": "telemetry",
                    "telemetry": sanitize_for_json(points),
                    "timestamp": now,
                }
                try:
                    await websocket.send_json(payload)
                    self._clients[websocket] = now
                except Exception:
                    self.disconnect(websocket)

    async def _broadcast(self, message: dict[str, Any]) -> None:
        for websocket in list(self._clients):
            try:
                await websocket.send_json(message)
            except Exception:
                self.disconnect(websocket)
