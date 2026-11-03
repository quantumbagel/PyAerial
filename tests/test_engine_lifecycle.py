"""Engine lifecycle robustness: stage failures, startup and shutdown,
signals, archive retries and the pending-finalize memory budget."""

from __future__ import annotations

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

import pyaerial.engine.archiver as archiver_mod
from helpers import (
    fine_track_plane,
    heading_plane,
    make_config,
    make_engine,
    one_fix_plane,
)
from pyaerial.config.schema import ArchiveConfig
from pyaerial.engine import Engine
from pyaerial.models import AircraftInfo, Datum, Plane, TrackState, Verdict
from pyaerial.store.archive.writer import FRAME_PRUNE_BATCH

# --- Main loop stage isolation ----------------------------------------------


def _run_ticks(engine: Engine, ticks: int, monkeypatch) -> None:
    """Run the real main loop (no receivers) until *ticks* ticks have started."""
    seen = {"n": 0}
    done = threading.Event()
    real = engine.supervisor.restart_dead

    def counting():
        seen["n"] += 1
        if seen["n"] >= ticks:
            done.set()
            engine.request_stop()
        real()

    monkeypatch.setattr(engine, "start_receivers", lambda: None)
    monkeypatch.setattr(engine.supervisor, "restart_dead", counting)
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
        archived: list[str] = []

        def finalize(plane, **_kwargs):
            if plane.info.icao == "bad":
                raise ValueError("unexpected")
            archived.append(plane.flight_id)
            return True

        monkeypatch.setattr(engine.history_store, "finalize_plane", finalize)
        assert not engine.archiver.finalize(one_fix_plane("bad"))
        assert engine.archiver.finalize(one_fix_plane("good"))
        assert archived == ["good-1"]
        # Kept (full in-memory copy) for retry.
        assert list(engine.archiver.pending) == ["bad-1"]
    finally:
        monkeypatch.undo()
        engine.archiver.clear_pending()
        engine.shutdown()


def test_alert_deactivation_failure_still_archives(tmp_path, monkeypatch):
    engine = make_engine(tmp_path)
    try:
        archived: list[str] = []

        def finalize(plane, **_kwargs):
            archived.append(plane.flight_id)
            return True

        def broken(one_fix_plane):
            raise RuntimeError("alerter bug")

        monkeypatch.setattr(engine.history_store, "finalize_plane", finalize)
        monkeypatch.setattr(engine.calculator, "deactivate_plane", broken)
        assert engine.archiver.finalize(one_fix_plane())
        assert archived == ["abc123-1"]
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
            engine.archiver.defer(f"{icao}-1", one_fix_plane(icao))

        engine.archiver.retry_pending()
        assert attempts == ["aaa"]
        engine.archiver.retry_pending()
        assert attempts == ["aaa"]  # backing off

        # After the back-off the next flight goes first, so one flight that
        # keeps failing cannot block the rest.
        engine.archiver.retry_now()
        engine.archiver.retry_pending()
        assert attempts == ["aaa", "bbb"]
        assert set(engine.archiver.pending) == {"aaa-1", "bbb-1", "ccc-1"}
    finally:
        monkeypatch.undo()
        engine.archiver.clear_pending()
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
    engine.archiver.defer("aaa-1", one_fix_plane("aaa"))
    engine.archiver.back_off()
    engine.shutdown()
    assert archived == ["aaa"]


# --- Raw frame pruning -------------------------------------------------------


def test_frame_prune_keeps_going_while_backlogged(tmp_path, monkeypatch):
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
    recorder = _Recorder()
    monkeypatch.setattr(engine.history_store, "write_frames", recorder.write_frames)
    monkeypatch.setattr(engine.history_store, "prune_frames", recorder.prune_frames)
    try:
        for _ in range(6):
            engine.supervisor.archive_frames([])
        # Three full batches, then the one that found the backlog cleared.
        assert recorder.pruned == 4
    finally:
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
        assert engine.stop_requested
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
    config = make_config()
    config.enrichment.enabled = False  # no hexdb.io lookup for abc123
    engine = Engine(config, isolated=True, aircraft_db_path=sys.argv[2])
    engine.start_receivers = lambda: None

    def finalize(plane, **_kwargs):
        if slow:
            time.sleep(60)
        print("archived", plane.info.icao, flush=True)
        return True

    engine.history_store.finalize_plane = finalize
    engine.tracker.planes["ABC123"] = heading_plane("abc123", time.time())
    engine.tracker.expired_planes = lambda now: []
    real_tick = engine.tick
    ready = []

    def tick():
        if not ready:
            ready.append(1)
            print("ready", flush=True)
        return real_tick()

    engine.tick = tick
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
        long_flight = archiver_mod.pending_cost(heading_plane("big", 1.0, samples=2_000))
        short_flight = archiver_mod.pending_cost(heading_plane("small", 1.0, samples=10))
        assert long_flight > 50 * short_flight  # cost tracks flight length
        monkeypatch.setattr(
            archiver_mod, "PENDING_FINALIZE_BUDGET_BYTES", long_flight + 3 * short_flight
        )
        # Far more short flights than the old count cap would allow per byte...
        engine.archiver.finalize(heading_plane("big", 1.0, samples=2_000))
        for n in range(3):
            engine.archiver.finalize(heading_plane(f"s{n:05d}", 1.0, samples=10))
        assert len(engine.archiver.pending) == 4
        assert engine.archiver.counters()["pending_evicted"] == 0
        # ...and over budget the oldest go first (their live copy stays).
        engine.archiver.finalize(heading_plane("s99999", 1.0, samples=10))
        assert "big-1" not in engine.archiver.pending
        assert engine.archiver.counters()["pending_evicted"] == 1
        assert len(engine.archiver.pending) == 4
    finally:
        monkeypatch.undo()
        engine.archiver.clear_pending()
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
    estimate = archiver_mod.pending_cost(plane)
    assert 0.7 * measured < estimate < 1.5 * measured
    # The default budget keeps far more than the old 256-flight cap of these.
    assert archiver_mod.PENDING_FINALIZE_BUDGET_BYTES // estimate >= 256


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
        engine.archiver.defer(f"p{n:05d}-1", fine_track_plane(f"p{n:05d}"))
    started = time.monotonic()
    engine.archiver.tick()  # capped at ARCHIVE_TICK_BUDGET
    assert time.monotonic() - started < archiver_mod.ARCHIVE_TICK_BUDGET + 0.1
    assert 0 < len(archived) < 40
    engine.shutdown()  # the final attempt is not capped
    assert len(archived) == 40
