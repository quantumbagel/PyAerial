from __future__ import annotations

import threading
import time

from pyaerial.constants import STORE_FIRST_PACKET, STORE_ICAO, STORE_INFO, STORE_INTERNAL
from pyaerial.engine import (
    Engine,
    _RECEIVER_BACKOFF_INITIAL,
    _RECEIVER_BACKOFF_MAX,
    _ReceiverHandle,
)
from helpers import make_config


class _DummyReceiver:
    def stop(self) -> None:
        return None


def _engine(tmp_path) -> Engine:
    return Engine(
        make_config(),
        isolated=True,
        aircraft_db_path=str(tmp_path / "aircraft.db"),
    )


def _dead_thread() -> threading.Thread:
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
    return thread


def _plane(icao: str = "abc123", first: float = 1.0) -> dict:
    return {
        STORE_INFO: {STORE_ICAO: icao},
        STORE_INTERNAL: {STORE_FIRST_PACKET: first},
    }


def test_dead_receiver_waits_before_restart(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    try:
        handle = _ReceiverHandle(
            name="mock",
            method="mock",
            receiver=_DummyReceiver(),
            thread=_dead_thread(),
            started_at=time.monotonic(),
            backoff=_RECEIVER_BACKOFF_INITIAL,
        )
        engine._receivers["mock"] = handle
        starts: list[float] = []
        monkeypatch.setattr(
            engine,
            "_start_receiver",
            lambda *args, **kwargs: starts.append(kwargs.get("backoff", 0)),
        )

        engine._restart_dead_receivers()
        assert starts == []
        assert handle.next_restart_at > time.monotonic()
        assert handle.backoff == _RECEIVER_BACKOFF_INITIAL * 2

        handle.next_restart_at = time.monotonic() - 0.01
        engine._restart_dead_receivers()
        assert starts == [_RECEIVER_BACKOFF_INITIAL * 2]
    finally:
        engine.shutdown()


def test_receiver_backoff_resets_after_long_run(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    try:
        handle = _ReceiverHandle(
            name="mock",
            method="mock",
            receiver=_DummyReceiver(),
            thread=_dead_thread(),
            started_at=time.monotonic() - 30.0,
            backoff=_RECEIVER_BACKOFF_MAX,
        )
        engine._receivers["mock"] = handle
        monkeypatch.setattr(engine, "_start_receiver", lambda *args, **kwargs: None)

        engine._restart_dead_receivers()
        assert handle.backoff == _RECEIVER_BACKOFF_INITIAL * 2
        delay = handle.next_restart_at - time.monotonic()
        assert 0.5 < delay <= _RECEIVER_BACKOFF_INITIAL + 0.5
    finally:
        engine.shutdown()


def test_receiver_backoff_caps(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    try:
        handle = _ReceiverHandle(
            name="mock",
            method="mock",
            receiver=_DummyReceiver(),
            thread=_dead_thread(),
            started_at=time.monotonic(),
            backoff=_RECEIVER_BACKOFF_MAX,
        )
        engine._receivers["mock"] = handle
        monkeypatch.setattr(engine, "_start_receiver", lambda *args, **kwargs: None)

        engine._restart_dead_receivers()
        assert handle.backoff == _RECEIVER_BACKOFF_MAX
    finally:
        engine.shutdown()


def test_alive_receiver_is_not_restarted(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    try:
        stop = threading.Event()

        def _idle():
            stop.wait(2.0)

        thread = threading.Thread(target=_idle)
        thread.start()
        handle = _ReceiverHandle(
            name="mock",
            method="mock",
            receiver=_DummyReceiver(),
            thread=thread,
            started_at=time.monotonic(),
        )
        engine._receivers["mock"] = handle
        starts: list[int] = []
        monkeypatch.setattr(
            engine, "_start_receiver", lambda *args, **kwargs: starts.append(1)
        )
        engine._restart_dead_receivers()
        assert starts == []
        stop.set()
        thread.join(timeout=2.0)
    finally:
        engine.shutdown()


def test_message_queue_drops_oldest(tmp_path):
    engine = _engine(tmp_path)
    try:
        import queue as queue_mod

        engine._message_queue = queue_mod.Queue(maxsize=2)
        engine._enqueue_message("aa", 1.0, "r")
        engine._enqueue_message("bb", 2.0, "r")
        engine._enqueue_message("cc", 3.0, "r")
        batch = engine._drain_messages()
        assert [msg for msg, _ts, _recv in batch] == ["bb", "cc"]
    finally:
        engine.shutdown()


def test_pending_finalize_is_capped(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    try:
        monkeypatch.setattr(engine.mongo_store, "finalize_plane", lambda *a, **k: False)
        popped: list[str] = []
        monkeypatch.setattr(
            engine.live_store, "pop_flight", lambda flight_id: popped.append(flight_id)
        )
        monkeypatch.setattr(engine.live_store, "get_alerts", lambda **k: [])
        monkeypatch.setattr(engine.calculator, "deactivate_plane", lambda plane: None)
        monkeypatch.setattr("pyaerial.engine._PENDING_FINALIZE_MAX", 2)

        engine._finalize_plane(_plane("aaa", 1.0))
        engine._finalize_plane(_plane("bbb", 1.0))
        engine._finalize_plane(_plane("ccc", 1.0))

        assert len(engine._pending_finalize) == 2
        assert "aaa-1" not in engine._pending_finalize
        assert "bbb-1" in engine._pending_finalize
        assert "ccc-1" in engine._pending_finalize
        assert "aaa-1" in popped
    finally:
        engine.shutdown()


def test_pending_finalize_retries_then_clears(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    try:
        monkeypatch.setattr(engine.calculator, "deactivate_plane", lambda plane: None)
        monkeypatch.setattr(engine.live_store, "get_alerts", lambda **k: [])
        popped: list[str] = []
        monkeypatch.setattr(
            engine.live_store, "pop_flight", lambda flight_id: popped.append(flight_id)
        )

        calls = {"n": 0}

        def _finalize(plane, alerts=None):
            calls["n"] += 1
            return calls["n"] > 1

        monkeypatch.setattr(engine.mongo_store, "finalize_plane", _finalize)
        plane = _plane()
        engine._finalize_plane(plane)
        assert engine._pending_finalize
        engine._retry_pending_finalizes()
        assert engine._pending_finalize == {}
        assert popped == ["abc123-1"]
    finally:
        engine.shutdown()
