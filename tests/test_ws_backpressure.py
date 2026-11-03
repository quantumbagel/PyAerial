"""WebSocket memory bounds: queued bytes, in-flight slots, the server-wide
budget and the per-IP request rate."""

from __future__ import annotations

import asyncio
import json
import time

from fastapi.testclient import TestClient

from helpers import make_config
from pyaerial.api import broadcaster as bc
from pyaerial.api.broadcaster import LiveBroadcaster, _Client, _Outbox, _Queued
from pyaerial.webapp import create_app


def _q(kind: str, size: int, *, done: asyncio.Future | None = None) -> _Queued:
    return _Queued(kind, "x" * size, done)


def test_outbox_caps_queued_bytes():
    async def _scenario() -> None:
        box = _Outbox(maxsize=100, max_bytes=1000)
        assert box.put(_q("response", 600))
        # Over the byte cap: droppable streams are skipped, responses refused.
        assert not box.put(_q("telemetry", 600))
        assert not box.put(_q("response", 600))
        assert box.queued_bytes == 600
        # One message bigger than the cap still goes when nothing is queued.
        empty = _Outbox(maxsize=100, max_bytes=1000)
        assert empty.put(_q("response", 5000))

    asyncio.run(_scenario())


def test_state_snapshots_replace_older_queued_copies():
    async def _scenario() -> list:
        box = _Outbox(maxsize=100, max_bytes=10_000)
        for kind in ("flights", "alerts", "flights", "stats", "flights"):
            assert box.put(_q(kind, 100))
        return box.types()

    assert asyncio.run(_scenario()) == ["alerts", "stats", "flights"]


class _SlowWS:
    """Accepts, then never finishes a send until released (a non-reader)."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.sent: list[str] = []

    async def accept(self) -> None:
        return None

    async def send_text(self, text: str) -> None:
        await self.release.wait()
        self.sent.append(text)


def test_response_future_resolves_only_after_the_send():
    async def _scenario() -> None:
        broadcaster = LiveBroadcaster(None, None)
        ws = _SlowWS()
        await broadcaster.connect(ws)
        sent = broadcaster.send_response(ws, json.dumps({"type": "response", "id": 1}))
        await asyncio.sleep(0.05)
        assert sent is not None and not sent.done()  # queued, not yet sent
        ws.release.set()
        assert await asyncio.wait_for(sent, 1) is True
        broadcaster.disconnect(ws)
        # After disconnect a response is not queued at all.
        assert broadcaster.send_response(ws, "{}") is None

    asyncio.run(_scenario())


def test_disconnect_resolves_pending_responses():
    async def _scenario() -> bool:
        broadcaster = LiveBroadcaster(None, None)
        ws = _SlowWS()
        await broadcaster.connect(ws)
        sent = broadcaster.send_response(ws, "{}")
        broadcaster.disconnect(ws)
        return await asyncio.wait_for(sent, 1)

    assert asyncio.run(_scenario()) is False


def test_server_budget_drops_the_biggest_backlog(monkeypatch):
    monkeypatch.setattr(bc, "_SERVER_QUEUE_BYTES", 10_000)

    async def _scenario() -> tuple[int, bool]:
        broadcaster = LiveBroadcaster(None, None)
        hog, polite = _SlowWS(), _SlowWS()
        await broadcaster.connect(hog)
        await broadcaster.connect(polite)
        broadcaster.send_response(hog, "h" * 6000)
        broadcaster.send_response(hog, "h" * 3000)
        await asyncio.sleep(0)
        # The polite client's response does not fit: the hog is dropped for it.
        ok = broadcaster.send_response(polite, "p" * 3000)
        await asyncio.sleep(0)
        return (
            broadcaster.client_count,
            ok is not None and polite in broadcaster._clients,
        )

    count, polite_kept = asyncio.run(_scenario())
    assert count == 1 and polite_kept


def test_in_flight_slot_is_held_until_the_response_is_sent(monkeypatch):
    """With inflight=1, a client that does not read gets one response queued."""
    config = make_config()
    config.web.ws_max_inflight = 1
    app = create_app(config=config)
    handled: list[str] = []

    def _fake_handler(action, params, **_kwargs):
        handled.append(action)
        return {"ok": True}

    monkeypatch.setattr("pyaerial.api.app.handle_ws_request", _fake_handler)
    broadcaster = app.state.broadcaster
    with (
        TestClient(app) as client,
        client.websocket_connect("/ws/live?streams=stats") as ws,
    ):
        ws.receive_json()  # hello
        ws.receive_json()  # stats snapshot: the client is registered after it
        # Stall the writer: every send blocks until released.
        gate = asyncio.Event()
        (ws_server,) = broadcaster._clients
        original = ws_server.send_text

        async def _stalled(text):
            await gate.wait()
            await original(text)

        ws_server.send_text = _stalled  # type: ignore[method-assign]
        for i in range(5):
            ws.send_json({"type": "request", "id": i, "action": "fetchZones"})
        deadline = time.monotonic() + 2
        while not handled and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)
        # Only the first request ran: its response is unsent, so the slot is held.
        assert handled == ["fetchZones"]
        ws_server.send_text = original  # type: ignore[method-assign]
        portal_loop = broadcaster._loop
        portal_loop.call_soon_threadsafe(gate.set)
        replies = []
        while len(replies) < 5:
            message = ws.receive_json()
            if message.get("type") == "response":
                replies.append(message["id"])
        assert replies == [0, 1, 2, 3, 4]


def test_rate_limit_is_shared_per_ip():
    config = make_config()
    config.web.ws_rate_limit = 1000
    config.web.ws_rate_burst = 1000
    # Burst 3, then a wait far over the 1 s the server will delay a request.
    config.web.ws_rate_limit_per_ip = 0.01
    config.web.ws_rate_burst_per_ip = 3
    app = create_app(config=config)
    with TestClient(app) as client:
        with (
            client.websocket_connect("/ws/live?streams=stats") as a,
            client.websocket_connect("/ws/live?streams=stats") as b,
        ):
            for ws in (a, b):
                ws.receive_json()
            codes = []
            for i in range(6):
                ws = (a, b)[i % 2]
                ws.send_json({"type": "request", "id": i, "action": "fetchZones"})
                while (reply := ws.receive_json()).get("type") != "response":
                    pass
                codes.append(reply.get("code"))
    # The two connections share one per-IP budget of 3.
    assert codes == [None, None, None, "rate_limited", "rate_limited", "rate_limited"]


def test_client_queue_count_and_bytes_bound_a_non_reader():
    async def _scenario() -> tuple[int, int]:
        broadcaster = LiveBroadcaster(None, None)
        ws = _SlowWS()
        await broadcaster.connect(ws)
        client = broadcaster._clients[ws]
        for _ in range(1000):
            client.enqueue({"type": "telemetry", "telemetry": [{"x": "y" * 1000}]})
        return len(client.outbox), client.outbox.queued_bytes

    count, size = asyncio.run(_scenario())
    assert count <= bc._CLIENT_QUEUE_MAX
    assert size <= bc._CLIENT_QUEUE_BYTES


def test_client_dataclass_still_constructs():
    client = _Client(telemetry_floor=0.0, last_ping=0.0)
    assert client.enqueue({"type": "ping"})


def test_close_quietly_swallows_an_already_gone_peer():
    from starlette.websockets import WebSocketDisconnect, WebSocketState

    class _Gone:
        client_state = WebSocketState.CONNECTED

        async def close(self, code: int, reason: str) -> None:
            raise WebSocketDisconnect(code=1006)

    asyncio.run(bc._close_quietly(_Gone(), 1013, "client too slow"))
