"""Dump1090-compatible Beast output and JSON raw-frame fan-out.

Corrected Mode S frames (after dual-receiver merge) are published on Redis
``live:raw``. This hub subscribes once and fans the same batch out as Beast
binary on ``/ws/beast`` plus an optional TCP listener, and as JSON text on
``/ws/raw``.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections import Counter
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from pyaerial.jsonutil import dumps as json_dumps
from pyaerial.receivers.frames import encode_beast_messages
import contextlib

log = logging.getLogger("pyaerial.webapp")

_CLIENT_QUEUE_MAX = 64
_RAW_QUEUE_MAX = 64
_STOP_GRACE_SECONDS = 2.0
#: A TCP feeder whose socket accepts nothing for this long is dropped.
_DRAIN_TIMEOUT_SECONDS = 10.0
#: A feeder whose queue stays full (it reads, but slower than the feed) for
#: this long is dropped rather than served an ever-staler, gappy stream.
_OVERFLOW_GRACE_SECONDS = 10.0
#: At most one WARNING per interval for refused feeders (the rest go to DEBUG).
_REFUSAL_LOG_INTERVAL = 60.0
# Dead peers (unplugged, NAT timeout) are found by TCP keepalive, not traffic.
_KEEPALIVE = (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 15), ("TCP_KEEPCNT", 4))


class _Feed(asyncio.Queue):
    """One client's queue, with overflow bookkeeping."""

    def __init__(self) -> None:
        super().__init__(maxsize=_CLIENT_QUEUE_MAX)
        self.overflowing_since: float | None = None
        #: Set when the hub cut this client off; its writer then closes.
        self.cut_off = False

    def cut(self) -> None:
        self.cut_off = True
        while not self.empty():
            self.get_nowait()
        self.put_nowait(None)


def _enable_keepalive(writer: asyncio.StreamWriter) -> None:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in _KEEPALIVE:
            option = getattr(socket, name, None)
            if option is not None:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)


class BeastHub:
    """Subscribe to ``live:raw`` and stream Beast bytes plus JSON raw frames."""

    def __init__(
        self,
        live_store: Any | None,
        *,
        host: str = "127.0.0.1",
        port: int | None = 30105,
        max_clients: int = 16,
        max_clients_per_ip: int | None = None,
    ):
        self.live_store = live_store
        self.host = host
        self.port = port
        self.max_clients = max(1, int(max_clients))
        self.max_clients_per_ip = max(1, int(max_clients_per_ip or self.max_clients))
        self._queues: set[_Feed] = set()
        self._json_queues: set[_Feed] = set()
        self._tcp_per_ip: Counter[str] = Counter()
        self._refusal_logged_at = -_REFUSAL_LOG_INTERVAL
        self._refusals_since_log = 0
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
        if store is not None:
            self._raw_queue = asyncio.Queue(maxsize=_RAW_QUEUE_MAX)
            loop = asyncio.get_running_loop()
            store.start_raw_pubsub(lambda payload: self._enqueue_raw(loop, payload))
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
        if self.live_store is not None:
            self.live_store.stop_raw_pubsub()
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
            except TimeoutError:
                log.warning("Beast TCP server did not close within %.0fs", _STOP_GRACE_SECONDS)
        if self._raw_task is not None:
            self._raw_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._raw_task
            self._raw_task = None
        for queue in list(self._queues):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)
        for queue in list(self._json_queues):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)

    def _enqueue_raw(
        self, loop: asyncio.AbstractEventLoop, payload: dict[str, Any]
    ) -> None:
        queue = self._raw_queue
        if queue is None:
            return

        def _put() -> None:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(payload)

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_put)

    async def _raw_loop(self) -> None:
        queue = self._raw_queue
        if queue is None:
            return
        while True:
            payload = await queue.get()
            messages = payload.get("messages") or []
            if not messages:
                continue
            try:
                data = encode_beast_messages(messages)
                if data:
                    self._broadcast(data)
            except Exception:
                log.exception("Beast encode/broadcast failed")
            try:
                self._broadcast_json(messages)
            except Exception:
                log.exception("Raw JSON broadcast failed")

    def _broadcast(self, data: bytes) -> None:
        self._fanout(self._queues, data)

    def _broadcast_json(self, messages: list[Any]) -> None:
        if not self._json_queues:
            return
        self._fanout(self._json_queues, json_dumps({"type": "raw", "frames": messages}))

    @staticmethod
    def _fanout(queues: set[_Feed], item: Any) -> None:
        now = time.monotonic()
        for queue in list(queues):
            if queue.cut_off:
                continue
            if queue.full():
                if queue.overflowing_since is None:
                    queue.overflowing_since = now
                elif now - queue.overflowing_since > _OVERFLOW_GRACE_SECONDS:
                    log.info("Beast/raw client cannot keep up with the feed; dropping it")
                    queue.cut()
                    continue
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            else:
                queue.overflowing_since = None
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(item)

    def _attach(self) -> _Feed:
        queue = _Feed()
        self._queues.add(queue)
        return queue

    def _detach(self, queue: _Feed) -> None:
        self._queues.discard(queue)

    def _attach_json(self) -> _Feed:
        queue = _Feed()
        self._json_queues.add(queue)
        return queue

    def _detach_json(self, queue: _Feed) -> None:
        self._json_queues.discard(queue)

    async def run_websocket(self, websocket: WebSocket) -> None:
        await self._run_client(websocket, json_mode=False)

    async def run_json_websocket(self, websocket: WebSocket) -> None:
        await self._run_client(websocket, json_mode=True)

    async def _run_client(self, websocket: WebSocket, *, json_mode: bool) -> None:
        await websocket.accept()
        queue = self._attach_json() if json_mode else self._attach()
        writer = asyncio.create_task(self._ws_writer(websocket, queue, json_mode=json_mode))
        label = "Raw WS" if json_mode else "Beast WS"
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") in {"websocket.disconnect", "websocket.close"}:
                    break
        except WebSocketDisconnect:
            pass
        except RuntimeError as exc:
            # receive() after the writer closed the socket on a send failure.
            log.debug("%s receive ended: %s", label, exc)
        finally:
            if json_mode:
                self._detach_json(queue)
            else:
                self._detach(queue)
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer

    async def _ws_writer(
        self, websocket: WebSocket, queue: _Feed, *, json_mode: bool = False
    ) -> None:
        label = "Raw WS" if json_mode else "Beast WS"
        detach = self._detach_json if json_mode else self._detach
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    if queue.cut_off:
                        detach(queue)
                        await _close_websocket(websocket, label)
                    return
                if websocket.client_state != WebSocketState.CONNECTED:
                    return
                if json_mode:
                    await websocket.send_text(chunk)
                else:
                    await websocket.send_bytes(chunk)
        except asyncio.CancelledError:
            raise
        except (WebSocketDisconnect, RuntimeError, OSError) as exc:
            log.debug("%s send failed (%s); detaching client", label, type(exc).__name__)
            detach(queue)
            if websocket.client_state == WebSocketState.CONNECTED:
                try:
                    await websocket.close()
                except (RuntimeError, OSError) as close_exc:
                    log.debug("%s close failed: %s", label, type(close_exc).__name__)

    async def _handle_tcp(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        ip = str(peer[0]) if isinstance(peer, tuple) and peer else "unknown"
        if len(self._tcp_clients) >= self.max_clients:
            self._log_refusal(peer, f"{len(self._tcp_clients)}/{self.max_clients} clients")
            writer.close()
            await _wait_closed(writer)
            return
        if self._tcp_per_ip[ip] >= self.max_clients_per_ip:
            self._log_refusal(peer, f"{self.max_clients_per_ip} clients from {ip}")
            writer.close()
            await _wait_closed(writer)
            return
        task = asyncio.current_task()
        assert task is not None
        self._tcp_clients[writer] = task
        self._tcp_per_ip[ip] += 1
        _enable_keepalive(writer)
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
            self._tcp_per_ip[ip] -= 1
            if self._tcp_per_ip[ip] <= 0:
                del self._tcp_per_ip[ip]
            self._detach(queue)
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            writer.close()
            await _wait_closed(writer)
            log.debug("Beast TCP client disconnected: %s", peer)

    def _log_refusal(self, peer: Any, reason: str) -> None:
        now = time.monotonic()
        if now - self._refusal_logged_at >= _REFUSAL_LOG_INTERVAL:
            suppressed = self._refusals_since_log
            log.warning(
                "Beast TCP client %s refused (%s)%s",
                peer,
                reason,
                f"; {suppressed} more refused since the last warning" if suppressed else "",
            )
            self._refusal_logged_at = now
            self._refusals_since_log = 0
        else:
            self._refusals_since_log += 1
            log.debug("Beast TCP client %s refused (%s)", peer, reason)

    async def _tcp_writer(self, writer: asyncio.StreamWriter, queue: _Feed) -> None:
        stuck = False
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    stuck = queue.cut_off
                    break
                writer.write(chunk)
                # A feeder that stops reading would otherwise block here (and
                # hold its slot) forever once the socket buffers fill.
                await asyncio.wait_for(writer.drain(), _DRAIN_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            stuck = True
            log.info(
                "Beast TCP client %s stopped reading; dropping it",
                writer.get_extra_info("peername"),
            )
        except (ConnectionError, OSError) as exc:
            log.debug("Beast TCP write failed (%s); detaching client", type(exc).__name__)
        # Stop fanning out to this client and close the transport so the
        # handler's read() returns EOF and it cleans up. A peer that is not
        # reading never lets close() flush the buffer: abort instead.
        self._detach(queue)
        if stuck:
            writer.transport.abort()
        else:
            writer.close()


async def _close_websocket(websocket: WebSocket, label: str) -> None:
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    try:
        await websocket.close(code=1013, reason="client too slow")
    except (RuntimeError, OSError, WebSocketDisconnect) as exc:
        log.debug("%s close failed: %s", label, type(exc).__name__)


async def _wait_closed(writer: asyncio.StreamWriter) -> None:
    try:
        await asyncio.wait_for(writer.wait_closed(), _STOP_GRACE_SECONDS)
    except TimeoutError:
        # Unflushed data to a peer that does not read: give up on it.
        writer.transport.abort()
    except (ConnectionError, OSError) as exc:
        log.debug("Beast TCP close: %s", type(exc).__name__)
