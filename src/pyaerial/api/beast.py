"""Dump1090-compatible Beast output for OpenSky Network and similar feeders.

Corrected Mode S frames (after dual-receiver merge) are re-encoded as Beast
binary and fanned out over ``/ws/beast`` plus an optional TCP listener that
speaks the same protocol dump1090 exposes on port 30005.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from pyaerial.receivers.frames import encode_beast_messages

log = logging.getLogger("pyaerial.webapp")

_CLIENT_QUEUE_MAX = 64
_RAW_QUEUE_MAX = 64


class BeastHub:
    """Subscribe to ``live:raw`` and stream Beast bytes to WS and TCP clients."""

    def __init__(
        self,
        live_store: Any | None,
        *,
        host: str = "0.0.0.0",
        port: int | None = 30005,
    ):
        self.live_store = live_store
        self.host = host
        self.port = port
        self._queues: set[asyncio.Queue] = set()
        self._raw_queue: asyncio.Queue | None = None
        self._raw_task: asyncio.Task | None = None
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        store = self.live_store
        start_pubsub = getattr(store, "start_raw_pubsub", None)
        if callable(start_pubsub):
            self._raw_queue = asyncio.Queue(maxsize=_RAW_QUEUE_MAX)
            loop = asyncio.get_running_loop()
            start_pubsub(lambda payload: self._enqueue_raw(loop, payload))
            self._raw_task = asyncio.create_task(self._raw_loop())
        if self.port is None:
            return
        try:
            self._server = await asyncio.start_server(
                self._handle_tcp, self.host, self.port
            )
        except OSError as exc:
            log.warning(
                "Beast TCP %s:%s unavailable (%s); /ws/beast still serves Beast",
                self.host,
                self.port,
                exc,
            )
            self._server = None
            self.port = None
            return
        sockets = self._server.sockets or []
        if sockets:
            self.port = int(sockets[0].getsockname()[1])
        log.info("Beast TCP listening on %s:%s (dump1090-compatible)", self.host, self.port)

    async def stop(self) -> None:
        store = self.live_store
        stop_pubsub = getattr(store, "stop_raw_pubsub", None)
        if callable(stop_pubsub):
            stop_pubsub()
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        if self._raw_task is not None:
            self._raw_task.cancel()
            try:
                await self._raw_task
            except asyncio.CancelledError:
                pass
            self._raw_task = None
        for queue in list(self._queues):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def _enqueue_raw(
        self, loop: asyncio.AbstractEventLoop, payload: dict[str, Any]
    ) -> None:
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
                messages = payload.get("messages") or []
                if not messages:
                    continue
                data = encode_beast_messages(messages)
                if data:
                    self._broadcast(data)
            except Exception:
                log.exception("Beast encode/broadcast failed")

    def _broadcast(self, data: bytes) -> None:
        for queue in list(self._queues):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                pass

    def _attach(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=_CLIENT_QUEUE_MAX)
        self._queues.add(queue)
        return queue

    def _detach(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)

    async def run_websocket(self, websocket: WebSocket) -> None:
        await websocket.accept()
        queue = self._attach()
        writer = asyncio.create_task(self._ws_writer(websocket, queue))
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") in {"websocket.disconnect", "websocket.close"}:
                    break
        except WebSocketDisconnect:
            pass
        finally:
            self._detach(queue)
            writer.cancel()
            try:
                await writer
            except asyncio.CancelledError:
                pass

    async def _ws_writer(self, websocket: WebSocket, queue: asyncio.Queue) -> None:
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    return
                if websocket.client_state != WebSocketState.CONNECTED:
                    return
                await websocket.send_bytes(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _handle_tcp(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        log.debug("Beast TCP client connected: %s", peer)
        queue = self._attach()
        pump = asyncio.create_task(self._tcp_writer(writer, queue))
        try:
            while True:
                inbound = await reader.read(1024)
                if not inbound:
                    break
        except Exception:
            pass
        finally:
            self._detach(queue)
            pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            log.debug("Beast TCP client disconnected: %s", peer)

    async def _tcp_writer(
        self, writer: asyncio.StreamWriter, queue: asyncio.Queue
    ) -> None:
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    return
                writer.write(chunk)
                await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
