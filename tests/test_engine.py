from __future__ import annotations

import json
import logging
import queue
import threading
import time

import redis
from pyModeS.util import crc as pms_crc

import pyaerial.engine.archiver as archiver_mod
import pyaerial.engine.supervisor as supervisor_mod
from fake_redis import FakeRedis
from helpers import (
    DF17_HEX,
    fine_track_plane,
    make_config,
    make_engine,
    pad_config,
)
from pyaerial.config.schema import ArchiveConfig
from pyaerial.engine import Engine
from pyaerial.engine.supervisor import (
    RECEIVER_BACKOFF_INITIAL,
    RECEIVER_BACKOFF_MAX,
    ReceiverHandle,
)
from pyaerial.models import AircraftInfo, Plane, TrackState
from pyaerial.receivers.frames import RawFrame


class _DummyReceiver:
    def stop(self) -> None:
        return None


def _dead_thread() -> threading.Thread:
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
    return thread


def _plane(icao: str = "abc123", first: float = 1.0) -> Plane:
    return Plane(info=AircraftInfo(icao=icao), track=TrackState(first, first))


def test_dead_receiver_waits_before_restart(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    try:
        handle = ReceiverHandle(
            name="main",
            method="dump1090",
            receiver=_DummyReceiver(),
            thread=_dead_thread(),
            started_at=time.monotonic(),
            backoff=RECEIVER_BACKOFF_INITIAL,
        )
        engine.supervisor.receivers["main"] = handle
        starts: list[float] = []
        monkeypatch.setattr(
            engine.supervisor,
            "start_receiver",
            lambda *args, **kwargs: starts.append(kwargs.get("backoff", 0)),
        )

        engine.supervisor.restart_dead()
        assert starts == []
        assert handle.next_restart_at > time.monotonic()
        assert handle.backoff == RECEIVER_BACKOFF_INITIAL * 2

        handle.next_restart_at = time.monotonic() - 0.01
        engine.supervisor.restart_dead()
        assert starts == [RECEIVER_BACKOFF_INITIAL * 2]
    finally:
        engine.shutdown()


def test_receiver_backoff_resets_after_long_run(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    try:
        handle = ReceiverHandle(
            name="main",
            method="dump1090",
            receiver=_DummyReceiver(),
            thread=_dead_thread(),
            started_at=time.monotonic() - 30.0,
            backoff=RECEIVER_BACKOFF_MAX,
        )
        engine.supervisor.receivers["main"] = handle
        monkeypatch.setattr(engine.supervisor, "start_receiver", lambda *args, **kwargs: None)

        engine.supervisor.restart_dead()
        assert handle.backoff == RECEIVER_BACKOFF_INITIAL * 2
        delay = handle.next_restart_at - time.monotonic()
        assert 0.5 < delay <= RECEIVER_BACKOFF_INITIAL + 0.5
    finally:
        engine.shutdown()


def test_receiver_backoff_caps(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    try:
        handle = ReceiverHandle(
            name="main",
            method="dump1090",
            receiver=_DummyReceiver(),
            thread=_dead_thread(),
            started_at=time.monotonic(),
            backoff=RECEIVER_BACKOFF_MAX,
        )
        engine.supervisor.receivers["main"] = handle
        monkeypatch.setattr(engine.supervisor, "start_receiver", lambda *args, **kwargs: None)

        engine.supervisor.restart_dead()
        assert handle.backoff == RECEIVER_BACKOFF_MAX
    finally:
        engine.shutdown()


def test_alive_receiver_is_not_restarted(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    try:
        stop = threading.Event()

        def _idle():
            stop.wait(2.0)

        thread = threading.Thread(target=_idle)
        thread.start()
        handle = ReceiverHandle(
            name="main",
            method="dump1090",
            receiver=_DummyReceiver(),
            thread=thread,
            started_at=time.monotonic(),
        )
        engine.supervisor.receivers["main"] = handle
        starts: list[int] = []
        monkeypatch.setattr(
            engine.supervisor, "start_receiver", lambda *args, **kwargs: starts.append(1)
        )
        engine.supervisor.restart_dead()
        assert starts == []
        stop.set()
        thread.join(timeout=2.0)
    finally:
        engine.shutdown()


def test_message_queue_drops_oldest(tmp_path):
    engine = make_engine(tmp_path)
    try:
        engine.supervisor.queue = queue.Queue(maxsize=2)
        engine.supervisor.enqueue(RawFrame("aa", 1.0, "r"))
        engine.supervisor.enqueue(RawFrame("bb", 2.0, "r"))
        engine.supervisor.enqueue(RawFrame("cc", 3.0, "r"))
        batch = engine.supervisor.drain()
        assert [frame.hex for frame in batch] == ["bb", "cc"]
    finally:
        engine.shutdown()


def test_pending_finalize_is_capped(tmp_path, monkeypatch, caplog):
    engine = make_engine(tmp_path)
    try:
        monkeypatch.setattr(
            engine.history_store, "finalize_plane", lambda *a, **k: False
        )
        popped: list[str] = []
        monkeypatch.setattr(
            engine.live_store, "pop_flight", lambda flight_id: popped.append(flight_id)
        )
        monkeypatch.setattr(engine.calculator, "deactivate_plane", lambda plane: None)
        monkeypatch.setattr(
            archiver_mod,
            "PENDING_FINALIZE_BUDGET_BYTES",
            2 * archiver_mod.pending_cost(_plane()),
        )

        with caplog.at_level("ERROR", logger="pyaerial.engine"):
            for icao in ("aaa", "bbb", "ccc", "ddd"):
                engine.archiver.finalize(_plane(icao, 1.0))

        # Regression: the "cap" only logged and kept growing.
        assert list(engine.archiver.pending) == ["ccc-1", "ddd-1"]
        assert engine.archiver.counters()["pending_evicted"] == 2
        assert caplog.text.count("memory budget") == 1
        # Nothing was archived, so no live copy was popped.
        assert popped == []
        # Re-enqueueing a pending flight updates it in place (no eviction).
        engine.archiver.defer("ddd-1", _plane("ddd", 1.0))
        assert list(engine.archiver.pending) == ["ccc-1", "ddd-1"]
    finally:
        engine.shutdown()


def test_retries_wait_while_history_is_down(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    try:
        monkeypatch.setattr(engine.history_store, "disabled", False)
        monkeypatch.setattr(engine.history_store, "ping", lambda: False)

        def _boom(*_args, **_kwargs):
            raise AssertionError("must not retry archive while history is down")

        monkeypatch.setattr(engine.history_store, "finalize_plane", _boom)
        monkeypatch.setattr(engine.history_store, "checkpoint", _boom)
        engine.archiver.defer("aaa-1", _plane("aaa", 1.0))
        engine.archiver.retry_pending()
        assert list(engine.archiver.pending) == ["aaa-1"]
        engine.archiver.clear_pending()
    finally:
        monkeypatch.undo()
        engine.shutdown()


def test_pending_finalize_retries_then_clears(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    try:
        monkeypatch.setattr(engine.calculator, "deactivate_plane", lambda plane: None)
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
        engine.archiver.finalize(plane)
        assert engine.archiver.pending
        engine.archiver.retry_pending()
        assert engine.archiver.pending == {}
        assert popped == ["abc123-1"]
    finally:
        engine.shutdown()


def test_yielded_shutdown_skips_finalize(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    try:
        engine.yielded_writer = True
        engine.tracker.planes["abc123"] = _plane()
        called: list[int] = []
        monkeypatch.setattr(
            engine.archiver, "finalize", lambda *args, **kwargs: called.append(1)
        )
        monkeypatch.setattr(engine.live_store, "clear_engine", lambda: called.append(2))
        engine.shutdown()
        assert called == []
        assert engine.tracker.planes == {}
    finally:
        engine.shutdown()


def test_isolated_engine_uses_memory_store(tmp_path):
    engine = make_engine(tmp_path)
    try:
        assert engine.live_store.memory_only is True
        assert engine.live_store.writer is True
        assert engine.live_store.ping() is True
        assert engine.history_store.disabled is True
    finally:
        engine.shutdown()


def test_finalize_closes_active_alerts(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    try:
        monkeypatch.setattr(engine.calculator, "deactivate_plane", lambda plane: None)
        # A deactivation that never reached the archive (an alerter bug).
        monkeypatch.setattr(engine.history_store, "disabled", False)
        monkeypatch.setattr(
            engine.history_store,
            "flight_alerts",
            lambda flight_id: [
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
        engine.archiver.finalize(_plane())
        assert captured[0][0]["active"] is False
        assert captured[0][0]["deactivated_at"] is not None
    finally:
        engine.shutdown()


def test_engine_publishes_raw_frames(tmp_path):
    engine = make_engine(tmp_path)
    try:
        seen: list[dict] = []
        engine.live_store.add_raw_listener(lambda payload: seen.append(payload))
        engine.supervisor.enqueue(
            RawFrame(
                "8d406b902015a678d4d220aa4bda",
                1.5,
                "main",
                rssi=-12.34,
                clock=9,
            )
        )
        engine.supervisor.publish_raw(engine.supervisor.drain())
        assert len(seen) == 1
        message = seen[0]["messages"][0]
        assert message["hex"] == "8d406b902015a678d4d220aa4bda"
        assert message["receiver"] == "main"
        assert message["rssi"] == -12.3
        assert message["clock"] == 9
        assert message["df"] == 17
        assert message["icao"] == "406b90"
        assert message["timestamp"] == 1.5
        assert "receivers" not in message
    finally:
        engine.shutdown()


def test_engine_publishes_receivers_from_heard_by(tmp_path):
    from pyaerial.receivers.frames import RawFrame

    engine = make_engine(tmp_path)
    try:
        seen: list[dict] = []
        engine.live_store.add_raw_listener(lambda payload: seen.append(payload))
        hex_msg = "8d406b902015a678d4d220aa4bda"
        now = time.time()
        outgoing, _, heard_by = engine.supervisor.merge(
            [
                RawFrame(
                    hex=hex_msg, timestamp=now, receiver="weak", rssi=-30.0, clock=1
                ),
                RawFrame(
                    hex=hex_msg, timestamp=now, receiver="strong", rssi=-8.0, clock=2
                ),
            ]
        )
        engine.supervisor.publish_raw(outgoing, heard_by)
        assert len(seen) == 1
        message = seen[0]["messages"][0]
        assert message["receiver"] == "strong"
        assert message["receivers"] == ["strong", "weak"]
        assert message["rssi"] == -8.0
        assert message["clock"] == 2
    finally:
        engine.shutdown()


def test_engine_outgoing_frames_prefer_stronger_receiver(tmp_path):
    from pyaerial.receivers.frames import RawFrame

    engine = make_engine(tmp_path)
    try:
        hex_msg = "8d406b902015a678d4d220aa4bda"
        now = time.time()
        outgoing, new_messages, heard_by = engine.supervisor.merge(
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
        assert heard_by == {hex_msg: ("strong", "weak")}
        again, _, _ = engine.supervisor.merge(
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
    engine = Engine(
        config, isolated=True, aircraft_db_path=str(tmp_path / "aircraft.db")
    )
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
        lat = engine.tracker.planes["40621D"].received_data["latitude"][-1].value
        assert abs(lat - 52.26) < 0.05
    finally:
        engine.request_stop()
        runner.join(timeout=5)


def test_engine_archives_every_receiver_copy_when_raw_frames_enabled(
    tmp_path, monkeypatch
):
    from pyaerial.config.schema import ArchiveConfig
    from pyaerial.receivers.frames import RawFrame

    class _Recorder:
        def __init__(self):
            self.frames: list = []
            self.pruned: list = []

        def write_frames(self, frames):
            self.frames.extend(frames)
            return True

        def prune_frames(self, before):
            self.pruned.append(before)
            return 0

    config = make_config(
        archive=ArchiveConfig(raw_frames=True, raw_frames_keep_hours=1)
    )
    engine = Engine(
        config, isolated=True, aircraft_db_path=str(tmp_path / "aircraft.db")
    )
    recorder = _Recorder()
    monkeypatch.setattr(engine.history_store, "write_frames", recorder.write_frames)
    monkeypatch.setattr(engine.history_store, "prune_frames", recorder.prune_frames)
    try:
        hex_msg = "8d406b902015a678d4d220aa4bda"
        copies = [
            RawFrame(hex=hex_msg, timestamp=1.0, receiver="a", rssi=-30.0),
            RawFrame(hex=hex_msg, timestamp=1.0, receiver="b", rssi=-8.0),
        ]
        engine.supervisor.archive_frames(copies)
        engine.supervisor.archive_frames(copies)
        assert recorder.frames == copies * 2
        assert len(recorder.pruned) == 1  # pruning is rate limited
        assert time.time() - 3600 - recorder.pruned[0] < 5
    finally:
        engine.shutdown()


# --- Heartbeat counters ------------------------------------------------------
# Cumulative operational counters since start.


def test_heartbeat_publishes_cumulative_counters(tmp_path, monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda *a, **k: client))
    config = make_config()
    config.database.path = str(tmp_path / "pyaerial.db")
    before = time.time()
    engine = Engine(config, aircraft_db_path=str(tmp_path / "aircraft.db"))
    try:
        old = time.time() - 60  # older than the merge window: not held back
        for n in range(3):
            engine.supervisor.enqueue(RawFrame(DF17_HEX, old + n, "main"))
        # A full queue drops frames.
        full = queue.Queue(maxsize=1)
        real_queue, engine.supervisor.queue = engine.supervisor.queue, full
        for n in range(3):
            engine.supervisor.enqueue(RawFrame(DF17_HEX, old + n, "main"))
        engine.supervisor.queue = real_queue

        # A failing stage.
        def boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(engine.integrity, "evaluate", boom)
        # A receiver restart.
        engine.supervisor.receivers["main"] = ReceiverHandle(
            name="main",
            method="dump1090",
            receiver=_DummyReceiver(),
            thread=_dead_thread(),
            started_at=time.monotonic(),
            next_restart_at=time.monotonic() - 1,
        )
        monkeypatch.setattr(engine.supervisor, "start_receiver", lambda *a, **k: None)
        # A pending-finalize eviction.
        monkeypatch.setattr(archiver_mod, "PENDING_FINALIZE_BUDGET_BYTES", 1)
        for icao in ("aaa001", "aaa002"):
            engine.archiver.defer(f"{icao}-1", _plane(icao))
        engine.archiver.back_off()  # not archived now
        engine.tracker.planes["BBB001"] = Plane(
            info=AircraftInfo(icao="bbb001"),
            track=TrackState(first_packet=time.time(), last_update=time.time()),
        )

        engine.tick()
        counters = json.loads(client.kv["live:engine"])["counters"]
        assert before <= counters["since"] <= time.time()
        assert counters["frames_received"] == 3
        assert counters["frames_dropped"] == 2
        assert counters["receiver_restarts"] == {"main": 1}
        assert counters["stage_errors"] == {"integrity": 1}
        assert counters["pending_evicted"] == 1
        # The frames above created one plane, plus the one added directly.
        assert counters["planes_tracked"] == len(engine.tracker.planes) == 2
        assert counters["messages_processed"] >= 0

        # Cumulative: a second tick adds, it does not reset.
        engine.tick()
        counters = json.loads(client.kv["live:engine"])["counters"]
        assert counters["stage_errors"] == {"integrity": 2}
        assert counters["frames_received"] == 3
    finally:
        engine.archiver.clear_pending()
        engine.shutdown()


# --- Raw feed CRC filtering --------------------------------------------------


def _with_parity(data_hex: str) -> str:
    """A 112-bit frame from 88 data bits with a correct CRC-24 parity field."""
    parity = pms_crc(data_hex + "000000")
    return f"{data_hex}{parity:06x}"


# --- CRC-failed extended squitters never reach the raw feed ------------------


def test_raw_feed_drops_only_crc_failed_extended_squitters(tmp_path, monkeypatch):
    tisb = _with_parity("92" + "abcdef" + "58c382d690c8ac")  # DF18 CF=2 (TIS-B)
    assert pms_crc(tisb) == 0
    corrupt = DF17_HEX[:-1] + ("0" if DF17_HEX[-1] != "0" else "1")
    surveillance = "5d4ca2d4e3d2a7"  # DF11; DF4/5/20/21 overlay the address
    comm_b = "a0001838ca3e51f0a8000047a6c4"  # DF20: parity is address-overlaid
    short_df17 = "8d406b902015a6"  # a 56-bit DF17 cannot be a valid squitter

    class _Recorder:
        def __init__(self):
            self.frames = []

        def write_frames(self, frames):
            self.frames.extend(frames)
            return True

        def prune_frames(self, before):
            return 0

    engine = make_engine(tmp_path, archive=ArchiveConfig(raw_frames=True))
    recorder = _Recorder()
    monkeypatch.setattr(engine.history_store, "write_frames", recorder.write_frames)
    monkeypatch.setattr(engine.history_store, "prune_frames", recorder.prune_frames)
    try:
        published: list[str] = []
        engine.live_store.add_raw_listener(
            lambda payload: published.extend(m["hex"] for m in payload["messages"])
        )
        # Older than the merge window, so nothing is held for the next tick.
        now = time.time() - 60
        sent = [DF17_HEX, tisb, corrupt, surveillance, comm_b, short_df17]
        for offset, hex_msg in enumerate(sent):
            # Seconds apart, so the corrupt copy is not merged with the good one.
            engine.supervisor.enqueue(RawFrame(hex_msg, now + offset, "main"))
        engine.tick()
        assert sorted(published) == sorted([DF17_HEX, tisb, surveillance, comm_b])
        # The research archive keeps every copy, corrupted ones included.
        assert [frame.hex for frame in recorder.frames] == sent
    finally:
        engine.shutdown()


# --- Shared CRC helper -------------------------------------------------------


def test_engine_uses_the_shared_crc_helper():
    from pyaerial.receivers import frames

    assert not hasattr(supervisor_mod, "_es_crc_failed")
    assert supervisor_mod.es_crc_ok is frames.es_crc_ok


# --- Queue sized from tracking.hz --------------------------------------------


def test_queue_holds_several_ticks_at_peak_rate():
    size = supervisor_mod.message_queue_size
    peak = supervisor_mod.PEAK_FRAMES_PER_SECOND
    assert size(2.0) >= 3 * peak / 2.0  # ~3 ticks at 2 Hz
    assert size(1000.0) == supervisor_mod.MESSAGE_QUEUE_MIN  # floor
    assert size(0.01) == supervisor_mod.MESSAGE_QUEUE_MAX  # ceiling
    # Bounded memory: ~250 B per queued frame.
    assert supervisor_mod.MESSAGE_QUEUE_MAX * 250 < 100 * 1024 * 1024


def test_engine_queue_absorbs_a_900_aircraft_tick(tmp_path):
    from pyaerial.config.schema import TrackingConfig

    engine = Engine(
        make_config(tracking=TrackingConfig(hz=2)),
        isolated=True,
        aircraft_db_path=str(tmp_path / "aircraft.db"),
    )
    try:
        # 900 aircraft x 4 receivers profiled at ~20.5k frames/s: one 0.5 s
        # tick plus a slow tick's worth of backlog.
        for n in range(25_000):
            engine.supervisor.enqueue(
                RawFrame("8d406b902015a678d4d220aa4bda", float(n), "r")
            )
        assert engine.supervisor.counters()["frames_dropped"] == 0
        assert engine.supervisor.queue.maxsize == supervisor_mod.message_queue_size(2)
    finally:
        engine.shutdown()


# --- Receiver copies straddling a tick ---------------------------------------


class _ManualClock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _straddle_engine(tmp_path, monkeypatch):
    clock = _ManualClock(1_000.0)
    monkeypatch.setattr(time, "time", clock)
    engine = Engine(
        make_config(), isolated=True, aircraft_db_path=str(tmp_path / "a.db")
    )
    ingested: list[dict] = []
    observed: list[list[str]] = []
    real_ingest = engine.tracker.ingest
    real_observe = engine.integrity.observe_frames

    def ingest(messages, receivers=None):
        ingested.append(
            {hx: tuple((receivers or {}).get(hx, ())) for hx, _ts in messages}
        )
        return real_ingest(messages, receivers=receivers)

    def observe(frames):
        observed.append([f.receiver for f in frames])
        return real_observe(frames)

    monkeypatch.setattr(engine.tracker, "ingest", ingest)
    monkeypatch.setattr(engine.integrity, "observe_frames", observe)
    return engine, clock, ingested, observed


def test_copies_straddling_a_tick_are_merged_together(tmp_path, monkeypatch):
    engine, clock, ingested, observed = _straddle_engine(tmp_path, monkeypatch)
    try:
        engine.supervisor.enqueue(RawFrame(DF17_HEX, 999.97, "a", rssi=-20.0))  # 30 ms old at the tick
        engine.tick()
        assert not any(DF17_HEX in batch for batch in ingested)  # held, not lost
        engine.supervisor.enqueue(RawFrame(DF17_HEX, 1_000.00, "b", rssi=-10.0))  # arrives after it
        clock.now = 1_000.5
        engine.tick()
        merged = [batch[DF17_HEX] for batch in ingested if DF17_HEX in batch]
        assert merged == [("b", "a")]  # both receivers, the stronger first
        assert ["a", "b"] in [sorted(batch) for batch in observed]  # TDOA sees both
    finally:
        engine.shutdown()


def test_older_copy_is_held_back_with_its_younger_partner(tmp_path, monkeypatch):
    engine, clock, ingested, _ = _straddle_engine(tmp_path, monkeypatch)
    try:
        engine.supervisor.enqueue(RawFrame(DF17_HEX, 999.89, "a", rssi=-20.0))  # older than the window
        engine.supervisor.enqueue(RawFrame(DF17_HEX, 999.95, "b", rssi=-10.0))  # its younger copy
        engine.tick()
        assert not any(DF17_HEX in batch for batch in ingested)
        clock.now = 1_000.5
        engine.tick()
        assert [batch[DF17_HEX] for batch in ingested if DF17_HEX in batch] == [("b", "a")]
    finally:
        engine.shutdown()


def test_old_and_future_frames_are_not_delayed(tmp_path, monkeypatch):
    engine, _clock, ingested, _ = _straddle_engine(tmp_path, monkeypatch)
    try:
        other = "8d4840d6202cc371c32ce0576098"
        engine.supervisor.enqueue(RawFrame(DF17_HEX, 999.5, "a"))  # older than the window
        engine.supervisor.enqueue(RawFrame(other, 1_005.0, "a"))  # skewed clock: waiting won't help
        engine.tick()
        seen = {hx for batch in ingested for hx in batch}
        assert {DF17_HEX, other} <= seen
        assert engine.supervisor.held == ()
    finally:
        engine.shutdown()


def test_shutdown_flushes_held_and_queued_frames(tmp_path, monkeypatch):
    engine, _clock, ingested, _ = _straddle_engine(tmp_path, monkeypatch)
    engine.supervisor.enqueue(RawFrame(DF17_HEX, 999.99, "a"))
    engine.tick()
    assert engine.supervisor.held
    engine.supervisor.enqueue(RawFrame("8d4840d6202cc371c32ce0576098", 1_000.0, "b"))
    engine.shutdown()
    seen = {hx for batch in ingested for hx in batch}
    assert {DF17_HEX, "8d4840d6202cc371c32ce0576098"} <= seen


def test_engine_heartbeat_publishes_history_health(fake, tmp_path):
    engine = Engine(pad_config(tmp_path), aircraft_db_path=str(tmp_path / "a.db"))
    try:
        engine.tick()
        beat = json.loads(fake.kv["live:engine"])
        assert beat["history"] == "ok"
        assert beat["history_pending"] == 0
        assert beat["history_error"] is None
        assert '"history":"ok"' in fake.kv["live:engine"]  # healthcheck grep
        engine.history_store.close()
        engine.history_store._last_connect_attempt = time.monotonic() + 60
        engine.archiver.defer("abc123-1", fine_track_plane())
        engine.tick()
        beat = json.loads(fake.kv["live:engine"])
        assert beat["history"] == "down"
        assert beat["history_pending"] == 1
    finally:
        engine.archiver.clear_pending()
        engine.shutdown()


# --- Status line counts the whole interval -----------------------------------


def test_status_line_counts_messages_over_the_interval(tmp_path, caplog):
    engine = Engine(
        make_config(), isolated=True, aircraft_db_path=str(tmp_path / "a.db")
    )
    try:
        # status_interval (10 s) after the last status at 0.0: no line yet.
        with caplog.at_level(logging.INFO, logger="pyaerial.engine"):
            engine.log_status(5, now=1.0)
            engine.log_status(7, now=2.0)
            engine.log_status(3, now=3_600.0)
        infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1 and "processed 15 msg(s)" in infos[0]
        with caplog.at_level(logging.INFO, logger="pyaerial.engine"):
            engine.log_status(2, now=7_200.0)
        assert "processed 2 msg(s)" in caplog.records[-1].getMessage()
    finally:
        engine.shutdown()
