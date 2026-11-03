"""Engine lifecycle robustness: stage failures, startup and shutdown,
signals, archive retries and the pending-finalize memory budget."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
import tracemalloc
from pathlib import Path

import pytest

import pyaerial.engine as engine_mod
from helpers import (
    fine_track_plane,
    heading_plane,
    make_config,
    make_engine,
    one_fix_plane,
    pad_config,
)
from pyaerial.config.schema import ArchiveConfig
from pyaerial.engine import Engine
from pyaerial.models import AircraftInfo, Datum, Plane, TrackState, Verdict
from pyaerial.store.archive.writer import FRAME_PRUNE_BATCH
from pyaerial.store.redis_live import RedisLiveStore

# --- Main loop stage isolation ----------------------------------------------


def _run_ticks(engine: Engine, ticks: int, monkeypatch) -> None:
    """Run the real main loop (no receivers) until *ticks* ticks have started."""
    seen = {"n": 0}
    done = threading.Event()
    real = engine._restart_dead_receivers

    def counting():
        seen["n"] += 1
        if seen["n"] >= ticks:
            done.set()
            engine._shutdown.set()
        real()

    monkeypatch.setattr(engine, "start_receivers", lambda: None)
    monkeypatch.setattr(engine, "_restart_dead_receivers", counting)
    engine.config.tracking.hz = 50
    runner = threading.Thread(target=engine.run, daemon=True)
    runner.start()
    assert done.wait(5.0), "main loop stopped ticking"
    runner.join(timeout=5.0)
    assert not runner.is_alive()


@pytest.mark.parametrize(
    "target",
    [
        "integrity.evaluate",
        "integrity.observe_frames",
        "calculator.calculate_all",
        "live_store.write_live_planes",
        "tracker.ingest",
    ],
)
def test_stage_exception_does_not_stop_tracking(tmp_path, monkeypatch, caplog, target):
    engine = make_engine(tmp_path)
    owner_name, method = target.split(".")
    owner = getattr(engine, owner_name)
    real = getattr(owner, method)
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, method, flaky)
    with caplog.at_level(logging.ERROR, logger="pyaerial.engine"):
        _run_ticks(engine, 6, monkeypatch)
    assert calls["n"] >= 5
    # One traceback, not one per tick.
    assert caplog.text.count("RuntimeError: boom") == 1


def test_failed_startup_still_shuts_down(tmp_path, monkeypatch):
    """No receiver could start: release the heartbeat and close the stores."""
    engine = make_engine(tmp_path)
    cleared: list[int] = []
    monkeypatch.setattr(engine.live_store, "clear_engine", lambda: cleared.append(1))

    def no_receivers():
        raise RuntimeError("no receivers could be started")

    monkeypatch.setattr(engine, "start_receivers", no_receivers)
    with pytest.raises(RuntimeError, match="no receivers"):
        engine.run()
    assert engine._shutdown_done is True
    assert cleared == [1]


def test_finalize_exception_keeps_flight_and_other_planes(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    try:
        popped: list[str] = []
        monkeypatch.setattr(engine.live_store, "pop_flight", popped.append)

        def finalize(plane, **_kwargs):
            if plane.info.icao == "bad":
                raise ValueError("unexpected")
            return True

        monkeypatch.setattr(engine.history_store, "finalize_plane", finalize)
        engine._finalize_plane(one_fix_plane("bad"))
        engine._finalize_plane(one_fix_plane("good"))
        assert popped == ["good-1"]
        # Kept (full in-memory copy) for retry; its live copy is not popped.
        assert list(engine._pending_finalize) == ["bad-1"]
    finally:
        monkeypatch.undo()
        engine._pending_finalize.clear()
        engine.shutdown()


def test_alert_deactivation_failure_still_archives(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    try:
        popped: list[str] = []
        monkeypatch.setattr(engine.live_store, "pop_flight", popped.append)

        def broken(one_fix_plane):
            raise RuntimeError("alerter bug")

        monkeypatch.setattr(engine.calculator, "deactivate_plane", broken)
        engine._finalize_plane(one_fix_plane())
        assert popped == ["abc123-1"]
    finally:
        monkeypatch.undo()
        engine.shutdown()


def test_shutdown_finishes_when_one_plane_fails(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    archived: list[str] = []

    def finalize(plane, **_kwargs):
        icao = plane.info.icao
        if icao == "bad":
            raise ValueError("unexpected")
        archived.append(icao)
        return True

    monkeypatch.setattr(engine.history_store, "finalize_plane", finalize)
    closed: list[str] = []
    monkeypatch.setattr(engine.live_store, "close", lambda: closed.append("live"))
    monkeypatch.setattr(engine.history_store, "close", lambda: closed.append("history"))
    engine.tracker.planes["BAD"] = one_fix_plane("bad")
    engine.tracker.planes["GOOD"] = one_fix_plane("good")
    engine.shutdown()
    assert archived == ["good"]
    assert closed == ["live", "history"]


def test_startup_archive_survives_a_bad_leftover(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    try:
        monkeypatch.setattr(
            engine.live_store,
            "planes_for_finalize",
            lambda: [one_fix_plane("bad"), one_fix_plane("good")],
        )
        archived: list[str] = []

        def finalize(plane, **_kwargs):
            if plane.info.icao == "bad":
                raise ValueError("unexpected")
            archived.append(plane.info.icao)
            return True

        monkeypatch.setattr(engine.history_store, "finalize_plane", finalize)
        engine._archive_startup_live_flights()
        assert archived == ["good"]
    finally:
        monkeypatch.undo()
        engine._pending_finalize.clear()
        engine.shutdown()


# --- Archive retry storms ----------------------------------------------------


def test_pending_retries_back_off_while_writes_fail(tmp_path, monkeypatch):
    """Disk full: ping works, every write fails. Don't retry 256 flights per tick."""
    engine = make_engine(tmp_path)
    try:
        monkeypatch.setattr(engine.history_store, "disabled", False)
        monkeypatch.setattr(engine.history_store, "ping", lambda: True)
        attempts: list[str] = []

        def failing(plane, **_kwargs):
            attempts.append(plane.info.icao)
            return False

        monkeypatch.setattr(engine.history_store, "finalize_plane", failing)
        for icao in ("aaa", "bbb", "ccc"):
            engine._pending_finalize[f"{icao}-1"] = one_fix_plane(icao)

        engine._retry_pending_finalizes()
        assert attempts == ["aaa"]
        engine._retry_pending_finalizes()
        assert attempts == ["aaa"]  # backing off

        # After the back-off the next flight goes first, so one flight that
        # keeps failing cannot block the rest.
        engine._history_retry_at = 0.0
        engine._retry_pending_finalizes()
        assert attempts == ["aaa", "bbb"]
        assert set(engine._pending_finalize) == {"aaa-1", "bbb-1", "ccc-1"}
    finally:
        monkeypatch.undo()
        engine._pending_finalize.clear()
        engine.shutdown()


def test_orphan_sweep_stops_at_first_failure(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    try:
        monkeypatch.setattr(engine.history_store, "disabled", False)
        monkeypatch.setattr(engine.history_store, "ping", lambda: True)
        monkeypatch.setattr(
            engine.live_store, "flight_ids", lambda: {f"o{n:05d}-1" for n in range(50)}
        )
        monkeypatch.setattr(
            engine.live_store,
            "plane_for_finalize",
            lambda flight_id: one_fix_plane(flight_id.split("-")[0]),
        )
        attempts: list[str] = []

        def failing(plane, **_kwargs):
            attempts.append(plane.info.icao)
            return False

        monkeypatch.setattr(engine.history_store, "finalize_plane", failing)
        engine._retry_orphan_live_flights()
        engine._retry_orphan_live_flights()
        assert len(attempts) == 1
    finally:
        monkeypatch.undo()
        engine._pending_finalize.clear()
        engine.shutdown()


def test_shutdown_retries_pending_despite_back_off(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    archived: list[str] = []
    monkeypatch.setattr(engine.history_store, "disabled", False)
    monkeypatch.setattr(engine.history_store, "ping", lambda: True)
    monkeypatch.setattr(
        engine.history_store,
        "finalize_plane",
        lambda plane, **_k: archived.append(plane.info.icao) or True,
    )
    engine._pending_finalize["aaa-1"] = one_fix_plane("aaa")
    engine._history_retry_at = time.monotonic() + 3600
    engine.shutdown()
    assert archived == ["aaa"]


# --- Raw frame pruning -------------------------------------------------------


def test_frame_prune_keeps_going_while_backlogged(tmp_path):
    """One batch per 10 minutes cannot keep up with a busy site."""

    class _Recorder:
        def __init__(self):
            self.pruned = 0
            self.backlog = 3

        def write_frames(self, frames):
            return True

        def prune_frames(self, before):
            self.pruned += 1
            if self.backlog:
                self.backlog -= 1
                return FRAME_PRUNE_BATCH
            return 0

    config_archive = ArchiveConfig(raw_frames=True, raw_frames_keep_hours=1)
    engine = make_engine(tmp_path, archive=config_archive)
    real = engine.history_store
    recorder = _Recorder()
    engine.history_store = recorder
    try:
        for _ in range(6):
            engine._archive_frames([])
        # Three full batches, then the one that found the backlog cleared.
        assert recorder.pruned == 4
    finally:
        engine.history_store = real
        engine.shutdown()


# --- Signals, forced exit and the pending-finalize memory budget -------------


ROOT = Path(__file__).resolve().parents[1]


# --- A second signal forces exit ---------------------------------------------


def test_first_signal_shuts_down_and_restores_default_handlers(tmp_path):
    engine = make_engine(tmp_path)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        engine._install_signal_handlers()
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert engine._shutdown.is_set()
        assert signal.getsignal(signal.SIGINT) is signal.SIG_DFL
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
        engine._restore_signal_handlers()
        assert signal.getsignal(signal.SIGINT) is previous[signal.SIGINT]
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)
        engine.shutdown()


_SIGNAL_SCRIPT = textwrap.dedent(
    """
    import sys, time
    from helpers import make_config
    from pyaerial.engine import Engine
    from helpers import heading_plane

    slow = sys.argv[1] == "slow"
    engine = Engine(make_config(), isolated=True, aircraft_db_path=sys.argv[2])
    engine.start_receivers = lambda: None

    def finalize(plane, **_kwargs):
        if slow:
            time.sleep(60)
        print("archived", plane.info.icao, flush=True)
        return True

    engine.history_store.finalize_plane = finalize
    engine.tracker.planes["ABC123"] = heading_plane("abc123", time.time())
    engine.tracker.expired_planes = lambda now: []
    real_tick = engine._tick
    ready = []

    def tick():
        if not ready:
            ready.append(1)
            print("ready", flush=True)
        return real_tick()

    engine._tick = tick
    engine.run()
    print("clean exit", flush=True)
    """
)


def _spawn(mode: str, tmp_path) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT / "tests")])
    proc = subprocess.Popen(
        [sys.executable, "-c", _SIGNAL_SCRIPT, mode, str(tmp_path / "aircraft.db")],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line.strip() == "ready" or not line:
            break
    return proc


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_first_sigint_archives_flights_then_exits_cleanly(tmp_path):
    proc = _spawn("fast", tmp_path)
    proc.send_signal(signal.SIGINT)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0, out
    assert "archived abc123" in out
    assert "clean exit" in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_second_signal_forces_exit_during_slow_shutdown(tmp_path):
    proc = _spawn("slow", tmp_path)
    proc.send_signal(signal.SIGTERM)
    time.sleep(1.0)  # now stuck in the 60 s archive write
    assert proc.poll() is None
    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=10)
    assert time.monotonic() - started < 5
    assert proc.returncode == -signal.SIGTERM


# --- Pending finalize is bounded by memory, not flight count -----------------


def test_pending_finalize_is_bounded_by_memory(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    try:
        monkeypatch.setattr(
            engine.history_store, "finalize_plane", lambda *a, **k: False
        )
        monkeypatch.setattr(engine.live_store, "pop_flight", lambda flight_id: None)
        long_flight = engine_mod._pending_cost(heading_plane("big", 1.0, samples=2_000))
        short_flight = engine_mod._pending_cost(heading_plane("small", 1.0, samples=10))
        assert long_flight > 50 * short_flight  # cost tracks flight length
        monkeypatch.setattr(
            engine_mod, "_PENDING_FINALIZE_BUDGET_BYTES", long_flight + 3 * short_flight
        )
        # Far more short flights than the old count cap would allow per byte...
        engine._finalize_plane(heading_plane("big", 1.0, samples=2_000))
        for n in range(3):
            engine._finalize_plane(heading_plane(f"s{n:05d}", 1.0, samples=10))
        assert len(engine._pending_finalize) == 4
        assert engine._pending_evicted == 0
        # ...and over budget the oldest go first (their live copy stays).
        engine._finalize_plane(heading_plane("s99999", 1.0, samples=10))
        assert "big-1" not in engine._pending_finalize
        assert engine._pending_evicted == 1
        assert len(engine._pending_finalize) == 4
    finally:
        monkeypatch.undo()
        engine._pending_finalize.clear()
        engine.shutdown()


def _tracker_like_plane(positions: int) -> Plane:
    """Series as the tracker and kinematics fill them: ~2 positions and ~2
    velocity messages a second, two receivers, a verdict a minute."""
    recv = {
        k: []
        for k in (
            "latitude",
            "longitude",
            "altitude",
            "horizontal_speed",
            "heading",
            "vertical_speed",
        )
    }
    calc = {"horizontal_speed": [], "heading": []}
    heard = {}
    for i in range(positions):
        t = 1.7e9 + i * 0.5
        for key in recv:
            recv[key].append(Datum(1000.0 + i * 1e-3 + len(key), t))
        for key in calc:
            calc[key].append(Datum(800.0 + i * 1e-3, t))
        heard[t] = ("north", "south")
    verdicts = [
        Verdict(
            1.7e9 + n * 60,
            0.9,
            "ok",
            (),
            {"kinematics": {"status": "pass", "detail": "ok"}},
        )
        for n in range(positions // 120)
    ]
    return Plane(
        info=AircraftInfo(icao="abc123"),
        track=TrackState(
            first_packet=1.7e9,
            last_update=1.7e9 + positions / 2,
            position_receivers=heard,
            integrity_log=verdicts,
        ),
        received_data=recv,
        calculated_data=calc,
    )


def test_pending_cost_tracks_measured_memory():
    tracemalloc.start()
    plane = _tracker_like_plane(3_600)  # a 30 min flight
    measured, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    estimate = engine_mod._pending_cost(plane)
    assert 0.7 * measured < estimate < 1.5 * measured
    # The default budget keeps far more than the old 256-flight cap of these.
    assert engine_mod._PENDING_FINALIZE_BUDGET_BYTES // estimate >= 256


# --- Catch-up after a history outage is spread over ticks --------------------


def test_catch_up_is_capped_per_tick_but_not_at_shutdown(tmp_path, monkeypatch):
    engine = Engine(
        make_config(), isolated=True, aircraft_db_path=str(tmp_path / "a.db")
    )
    archived: list[str] = []

    def slow_finalize(plane, **_kwargs):
        time.sleep(0.02)
        archived.append(plane.info.icao)
        return True

    monkeypatch.setattr(engine.history_store, "finalize_plane", slow_finalize)
    for n in range(40):
        engine._pending_finalize[f"p{n:05d}-1"] = fine_track_plane(f"p{n:05d}")
    started = time.monotonic()
    engine._catch_up_archive(time.monotonic() + engine_mod._ARCHIVE_TICK_BUDGET)
    assert time.monotonic() - started < engine_mod._ARCHIVE_TICK_BUDGET + 0.1
    assert 0 < len(archived) < 40
    engine.shutdown()  # the final attempt is not capped
    assert len(archived) == 40


def test_startup_archive_keeps_the_heartbeat_fresh(fake, tmp_path, monkeypatch):
    t = time.time() - 300
    for n in range(5):
        fid = f"a{n:05d}-{int(t)}"
        fake.sets.setdefault("live:flights", set()).add(fid)
        fake.kv[f"live:flight:{fid}"] = json.dumps(
            {
                "flight_id": fid,
                "icao": f"a{n:05d}",
                "start_time": t,
                "end_time": t + 9,
                "info": {},
            }
        )
    beats: list[float] = []
    real_touch = RedisLiveStore.touch_engine

    def touch(self):
        beats.append(time.monotonic())
        return real_touch(self)

    monkeypatch.setattr(RedisLiveStore, "touch_engine", touch)
    monkeypatch.setattr(engine_mod, "_STARTUP_HEARTBEAT_INTERVAL", 0.0)
    engine = Engine(pad_config(tmp_path), aircraft_db_path=str(tmp_path / "a.db"))
    try:
        assert len(beats) >= 5
    finally:
        engine.shutdown()
