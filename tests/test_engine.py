from __future__ import annotations

import threading
import time

from pyaerial.constants import (
    STORE_FIRST_PACKET,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
)
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
            name="main",
            method="dump1090",
            receiver=_DummyReceiver(),
            thread=_dead_thread(),
            started_at=time.monotonic(),
            backoff=_RECEIVER_BACKOFF_INITIAL,
        )
        engine._receivers["main"] = handle
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
            name="main",
            method="dump1090",
            receiver=_DummyReceiver(),
            thread=_dead_thread(),
            started_at=time.monotonic() - 30.0,
            backoff=_RECEIVER_BACKOFF_MAX,
        )
        engine._receivers["main"] = handle
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
            name="main",
            method="dump1090",
            receiver=_DummyReceiver(),
            thread=_dead_thread(),
            started_at=time.monotonic(),
            backoff=_RECEIVER_BACKOFF_MAX,
        )
        engine._receivers["main"] = handle
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
            name="main",
            method="dump1090",
            receiver=_DummyReceiver(),
            thread=thread,
            started_at=time.monotonic(),
        )
        engine._receivers["main"] = handle
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
        assert [frame.hex for frame in batch] == ["bb", "cc"]
    finally:
        engine.shutdown()


def test_pending_finalize_is_capped(tmp_path, monkeypatch, caplog):
    engine = _engine(tmp_path)
    try:
        monkeypatch.setattr(
            engine.history_store, "finalize_plane", lambda *a, **k: False
        )
        popped: list[str] = []
        monkeypatch.setattr(
            engine.live_store, "pop_flight", lambda flight_id: popped.append(flight_id)
        )
        monkeypatch.setattr(engine.live_store, "get_alerts", lambda **k: [])
        monkeypatch.setattr(engine.calculator, "deactivate_plane", lambda plane: None)
        monkeypatch.setattr("pyaerial.engine._PENDING_FINALIZE_MAX", 2)

        with caplog.at_level("ERROR", logger="pyaerial.engine"):
            for icao in ("aaa", "bbb", "ccc", "ddd"):
                engine._finalize_plane(_plane(icao, 1.0))

        # Regression: the "cap" only logged and kept growing.
        assert list(engine._pending_finalize) == ["ccc-1", "ddd-1"]
        assert engine._pending_evicted == 2
        assert caplog.text.count("Pending finalize cap") == 1
        # Evicted flights keep their live copy for the orphan sweep.
        assert popped == []
        # Re-enqueueing a pending flight updates it in place (no eviction).
        engine._enqueue_pending_finalize("ddd-1", _plane("ddd", 1.0))
        assert list(engine._pending_finalize) == ["ccc-1", "ddd-1"]
    finally:
        engine.shutdown()


def test_retries_wait_while_history_is_down(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    try:
        monkeypatch.setattr(engine.history_store, "disabled", False)
        monkeypatch.setattr(engine.history_store, "ping", lambda: False)
        monkeypatch.setattr(engine.live_store, "flight_ids", lambda: {"zzz-1"})

        def _boom(*_args, **_kwargs):
            raise AssertionError("must not retry archive while history is down")

        monkeypatch.setattr(engine.live_store, "plane_for_finalize", _boom)
        monkeypatch.setattr(engine.history_store, "finalize_plane", _boom)
        engine._pending_finalize["aaa-1"] = _plane("aaa", 1.0)
        engine._retry_pending_finalizes()
        engine._retry_orphan_live_flights()
        assert list(engine._pending_finalize) == ["aaa-1"]
        engine._pending_finalize.clear()
    finally:
        monkeypatch.undo()
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

        def _finalize(plane, alerts=None, **_kwargs):
            calls["n"] += 1
            return calls["n"] > 1

        monkeypatch.setattr(engine.history_store, "finalize_plane", _finalize)
        plane = _plane()
        engine._finalize_plane(plane)
        assert engine._pending_finalize
        engine._retry_pending_finalizes()
        assert engine._pending_finalize == {}
        assert popped == ["abc123-1"]
    finally:
        engine.shutdown()


def test_yielded_shutdown_skips_finalize(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    try:
        engine._yielded_writer = True
        engine.tracker.planes["abc123"] = _plane()
        called: list[int] = []
        monkeypatch.setattr(
            engine, "_finalize_plane", lambda *args, **kwargs: called.append(1)
        )
        monkeypatch.setattr(engine.live_store, "clear_engine", lambda: called.append(2))
        engine.shutdown()
        assert called == []
        assert engine.tracker.planes == {}
    finally:
        engine.shutdown()


def test_isolated_engine_uses_memory_store(tmp_path):
    engine = _engine(tmp_path)
    try:
        assert engine.live_store.memory_only is True
        assert engine.live_store.writer is True
        assert engine.live_store.ping() is True
        assert engine.history_store.disabled is True
    finally:
        engine.shutdown()


def test_finalize_closes_active_alerts(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    try:
        monkeypatch.setattr(engine.calculator, "deactivate_plane", lambda plane: None)
        monkeypatch.setattr(
            engine.live_store,
            "get_alerts",
            lambda **k: [
                {
                    "alert_id": "abc123-1:pad:warn",
                    "active": True,
                    "deactivated_at": None,
                }
            ],
        )
        captured: list[list] = []

        def _finalize(plane, alerts=None, **_kwargs):
            captured.append(list(alerts or []))
            return True

        monkeypatch.setattr(engine.history_store, "finalize_plane", _finalize)
        monkeypatch.setattr(engine.live_store, "pop_flight", lambda flight_id: None)
        engine._finalize_plane(_plane())
        assert captured[0][0]["active"] is False
        assert captured[0][0]["deactivated_at"] is not None
    finally:
        engine.shutdown()


def test_engine_publishes_raw_frames(tmp_path):
    engine = _engine(tmp_path)
    try:
        seen: list[dict] = []
        engine.live_store.add_raw_listener(lambda payload: seen.append(payload))
        engine._enqueue_message(
            "8d406b902015a678d4d220aa4bda",
            1.5,
            "main",
            rssi=-12.34,
            clock=9,
        )
        engine._publish_raw(engine._drain_messages())
        assert len(seen) == 1
        message = seen[0]["messages"][0]
        assert message["hex"] == "8d406b902015a678d4d220aa4bda"
        assert message["receiver"] == "main"
        assert message["rssi"] == -12.3
        assert message["clock"] == 9
        assert message["df"] == 17
        assert message["icao"] == "406b90"
        assert message["timestamp"] == 1.5
    finally:
        engine.shutdown()


def test_engine_outgoing_frames_prefer_stronger_receiver(tmp_path):
    from pyaerial.receivers.frames import RawFrame

    engine = _engine(tmp_path)
    try:
        hex_msg = "8d406b902015a678d4d220aa4bda"
        now = time.time()
        outgoing, new_messages = engine._outgoing_frames(
            [
                RawFrame(
                    hex=hex_msg, timestamp=now, receiver="weak", rssi=-30.0, clock=1
                ),
                RawFrame(
                    hex=hex_msg, timestamp=now, receiver="strong", rssi=-8.0, clock=2
                ),
            ]
        )
        assert len(outgoing) == 1
        assert outgoing[0].receiver == "strong"
        assert outgoing[0].clock == 2
        assert new_messages == [(hex_msg, now)]
        again, _ = engine._outgoing_frames(
            [RawFrame(hex=hex_msg, timestamp=now + 0.1, receiver="weak", rssi=-4.0)]
        )
        assert again == []
    finally:
        engine.shutdown()


def test_engine_scores_integrity_into_live_flights(tmp_path):
    from pyaerial.config.schema import ReceiverConfig, ReceiverPositionConfig

    capture = tmp_path / "capture.txt"
    capture.write_text(
        "*8D40621D58C382D690C8AC2863A7;\n*8D40621D58C386435CC412692AD6;\n"
    )
    config = make_config(
        receivers={
            "nl": ReceiverConfig(
                type="replay",
                options={"path": str(capture), "interval": 0.05},
                position=ReceiverPositionConfig(latitude=52.0, longitude=4.0),
            )
        }
    )
    engine = Engine(config, isolated=True, aircraft_db_path=str(tmp_path / "aircraft.db"))
    runner = threading.Thread(target=engine.run, daemon=True)
    runner.start()
    try:
        deadline = time.monotonic() + 5
        doc = None
        while time.monotonic() < deadline and doc is None:
            flights = list(engine.live_store._mem.flights.values())
            if flights and "integrity" in flights[0]["info"]:
                doc = flights[0]
            time.sleep(0.05)
        assert doc is not None
        assert doc["icao"] == "40621d"
        assert doc["info"]["integrity"]["status"] in {"ok", "unknown"}
        # Decoded against the antenna in the Netherlands, not North Carolina home.
        lat = engine.tracker.planes["40621D"]["received_data"]["latitude"][-1].value
        assert abs(lat - 52.26) < 0.05
    finally:
        engine._shutdown.set()
        runner.join(timeout=5)
