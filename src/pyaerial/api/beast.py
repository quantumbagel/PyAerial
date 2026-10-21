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
_STOP_GRACE_SECONDS = 2.0


class BeastHub:
    """Subscribe to ``live:raw`` and stream Beast bytes to WS and TCP clients."""

    def __init__(
        self,
        live_store: Any | None,
        *,
        host: str = "127.0.0.1",
        port: int | None = 30005,
        max_clients: int = 16,
    ):
        self.live_store = live_store
        self.host = host
        self.port = port
        self.max_clients = max(1, int(max_clients))
        self._queues: set[asyncio.Queue] = set()
        self._raw_queue: asyncio.Queue | None = None
        self._raw_task: asyncio.Task | None = None
        self._server: asyncio.Server | None = None
        #: Live TCP connections: writer -> handler task.
        self._tcp_clients: dict[asyncio.StreamWriter, asyncio.Task] = {}

    @property
    def tcp_client_count(self) -> int:
        return len(self._tcp_clients)

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
        # wait_closed() waits for every connection handler, and handlers block
        # in reader.read() while a feeder idles. Close their transports first
        # (read() then returns EOF), and cancel any that still linger.
        clients = list(self._tcp_clients.items())
        for writer, _task in clients:
            writer.close()
        handlers = [task for _writer, task in clients]
        if handlers:
            _done, pending = await asyncio.wait(handlers, timeout=_STOP_GRACE_SECONDS)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if server is not None:
            try:
                await asyncio.wait_for(server.wait_closed(), _STOP_GRACE_SECONDS)
            except asyncio.TimeoutError:
                log.warning("Beast TCP server did not close within %.0fs", _STOP_GRACE_SECONDS)
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
        except RuntimeError as exc:
            # receive() after the writer closed the socket on a send failure.
            log.debug("Beast WS receive ended: %s", exc)
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
        except (WebSocketDisconnect, RuntimeError, OSError) as exc:
            log.debug("Beast WS send failed (%s); detaching client", type(exc).__name__)
            self._detach(queue)
            if websocket.client_state == WebSocketState.CONNECTED:
                try:
                    await websocket.close()
                except (RuntimeError, OSError) as close_exc:
                    log.debug("Beast WS close failed: %s", type(close_exc).__name__)

    async def _handle_tcp(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        if len(self._tcp_clients) >= self.max_clients:
            log.warning(
                "Beast TCP client %s refused: %d/%d clients connected",
                peer,
                len(self._tcp_clients),
                self.max_clients,
            )
            writer.close()
            await _wait_closed(writer)
            return
        task = asyncio.current_task()
        assert task is not None
        self._tcp_clients[writer] = task
        log.debug("Beast TCP client connected: %s", peer)
        queue = self._attach()
        pump = asyncio.create_task(self._tcp_writer(writer, queue))
        try:
            while True:
                inbound = await reader.read(1024)
                if not inbound:
                    break
        except (ConnectionError, OSError) as exc:
            log.debug("Beast TCP client %s read failed: %s", peer, type(exc).__name__)
        finally:
            self._tcp_clients.pop(writer, None)
            self._detach(queue)
            pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                pass
            writer.close()
            await _wait_closed(writer)
            log.debug("Beast TCP client disconnected: %s", peer)

    async def _tcp_writer(
        self, writer: asyncio.StreamWriter, queue: asyncio.Queue
    ) -> None:
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                writer.write(chunk)
                await writer.drain()
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as exc:
            log.debug("Beast TCP write failed (%s); detaching client", type(exc).__name__)
        # Stop fanning out to this client and close the transport so the
        # handler's read() returns EOF and it cleans up.
        self._detach(queue)
        writer.close()


async def _wait_closed(writer: asyncio.StreamWriter) -> None:
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError) as exc:
        log.debug("Beast TCP close: %s", type(exc).__name__)
