"""Beast TCP server: per-IP cap, quiet refusals, and non-readers lose their slot."""

from __future__ import annotations

import asyncio
import logging
import socket

from pyaerial.api import beast
from pyaerial.api.beast import BeastHub


async def _wait_for(predicate, seconds: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def test_per_ip_cap_and_quiet_refusals(caplog):
    async def _scenario() -> tuple[int, list[bytes]]:
        hub = BeastHub(
            None, host="127.0.0.1", port=0, max_clients=16, max_clients_per_ip=2
        )
        await hub.start()
        try:
            kept = [
                await asyncio.open_connection("127.0.0.1", hub.port) for _ in range(2)
            ]
            await _wait_for(lambda: hub.tcp_client_count == 2)
            refused = []
            for _ in range(20):
                reader, writer = await asyncio.open_connection("127.0.0.1", hub.port)
                refused.append(await asyncio.wait_for(reader.read(), 2))
                writer.close()
            count = hub.tcp_client_count
            for _reader, writer in kept:
                writer.close()
            return count, refused
        finally:
            await hub.stop()

    with caplog.at_level(logging.INFO, logger="pyaerial.webapp"):
        count, refused = asyncio.run(_scenario())
    assert count == 2
    assert refused == [b""] * 20
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) <= 1  # one per interval, not one per connection


def test_non_reading_client_is_dropped_and_readers_keep_up(monkeypatch):
    monkeypatch.setattr(beast, "_DRAIN_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(beast, "_OVERFLOW_GRACE_SECONDS", 0.5)

    async def _scenario() -> tuple[int, int, bool]:
        hub = BeastHub(None, host="127.0.0.1", port=0)
        await hub.start()
        try:
            stalled = socket.socket()
            stalled.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            stalled.connect(("127.0.0.1", hub.port))
            reader, writer = await asyncio.open_connection("127.0.0.1", hub.port)
            await _wait_for(lambda: hub.tcp_client_count == 2)
            received = 0

            async def _read() -> None:
                nonlocal received
                while chunk := await reader.read(65536):
                    received += len(chunk)

            read_task = asyncio.create_task(_read())
            chunk = b"\x1a\x33" + b"\x00" * 21
            for _ in range(300):
                hub._broadcast(chunk * 200)
                await asyncio.sleep(0.01)
            dropped = await _wait_for(lambda: hub.tcp_client_count == 1)
            remaining = hub.tcp_client_count
            writer.close()
            read_task.cancel()
            stalled.close()
            return remaining, received, dropped
        finally:
            await hub.stop()

    remaining, received, dropped = asyncio.run(_scenario())
    assert dropped and remaining == 1  # the non-reader lost its slot
    assert received > 1_000_000  # the reader got the stream meanwhile


def test_accepted_sockets_use_tcp_keepalive():
    async def _scenario() -> int:
        hub = BeastHub(None, host="127.0.0.1", port=0)
        await hub.start()
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", hub.port)
            await _wait_for(lambda: hub.tcp_client_count == 1)
            (server_side,) = hub._tcp_clients
            sock = server_side.get_extra_info("socket")
            value = sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
            writer.close()
            return value
        finally:
            await hub.stop()

    assert asyncio.run(_scenario()) != 0
