from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest
import requests

import pyaerial
from helpers import IDENT
from pyaerial.config.loader import load_config
from pyaerial.receivers.frames import (
    BEAST_ESC,
    BEAST_MLAT_CLOCK,
    beast_rssi_dbfs,
)
from pyaerial.receivers.replay import ReplayReceiver


def test_replay_loads_timestamped_and_bare_hex(tmp_path):
    path = tmp_path / "raw.txt"
    path.write_text("# comment\n1.0 AABBCC\nDDEEFF\n")
    receiver = ReplayReceiver(
        "replay", lambda *_args: None, {"path": str(path), "loop": False}
    )
    frames = receiver._load()
    assert len(frames) == 2
    assert frames[0][1] == "aabbcc"
    assert frames[1][1] == "ddeeff"


def test_replay_loads_avr_star_and_clock_lines(tmp_path):
    path = tmp_path / "avr.txt"
    path.write_text("*8DABCDEF000000;\n@0000000000018DABCDEF000001;\n")
    receiver = ReplayReceiver(
        "replay", lambda *_args: None, {"path": str(path), "loop": False}
    )
    frames = receiver._load()
    assert len(frames) == 2
    assert frames[0][1].startswith("8dabcdef")
    assert frames[1][1].startswith("8dabcdef")


def test_replay_paces_avr_clock_ticks(tmp_path):
    path = tmp_path / "avr.txt"
    # 12 MHz ticks 0 then 12_000_000 (= 1.0s)
    path.write_text("@0000000000008DABCDEF000000;\n@000000B71B008DABCDEF000001;\n")
    receiver = ReplayReceiver(
        "replay", lambda *_args: None, {"path": str(path), "loop": False}
    )
    frames = receiver._load()
    assert len(frames) == 2
    assert frames[0][0] == 0.0
    assert frames[1][0] == pytest.approx(1.0)


def test_replay_default_interval_spaces_untimestamped_lines(tmp_path):
    path = tmp_path / "raw.txt"
    path.write_text("AABBCC\nDDEEFF\n")
    receiver = ReplayReceiver(
        "replay", lambda *_args: None, {"path": str(path), "loop": False}
    )
    assert receiver.interval == 0.1
    frames = receiver._load()
    assert [frame.stamp for frame in frames] == [0.0, 0.1]


def test_replay_missing_file():
    receiver = ReplayReceiver(
        "replay", lambda *_args: None, {"path": "/no/such/file.txt", "loop": False}
    )
    reason = receiver.run()
    assert reason and "not found" in reason


def test_replay_loop_false_waits_until_stop(tmp_path):
    path = tmp_path / "raw.txt"
    path.write_text("AABBCC\n")
    emitted: list[str] = []
    receiver = ReplayReceiver(
        "replay",
        lambda hex_msg, _ts, **_kw: emitted.append(hex_msg),
        {"path": str(path), "loop": False, "interval": 0},
    )
    thread = threading.Thread(target=receiver.run)
    thread.start()
    thread.join(timeout=0.4)
    assert thread.is_alive()
    receiver.stop()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert emitted


def test_replay_quoted_false_does_not_loop(tmp_path):
    path = tmp_path / "raw.txt"
    path.write_text("AABBCC\n")
    receiver = ReplayReceiver(
        "replay",
        lambda *_args: None,
        {"path": str(path), "loop": "false", "interval": 0},
    )
    assert receiver.loop is False


def test_replay_rejects_non_positive_speed(tmp_path):
    path = tmp_path / "raw.txt"
    path.write_text("AABBCC\n")
    try:
        ReplayReceiver("replay", lambda *_args: None, {"path": str(path), "speed": 0})
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "speed" in str(exc)


# --- Replay pacing ----------------------------------------------------------


def _replay_frames(tmp_path, text: str) -> list[tuple[float, str]]:
    path = tmp_path / "avr.txt"
    path.write_text(text)
    receiver = ReplayReceiver(
        "replay", lambda *_args: None, {"path": str(path), "loop": False}
    )
    return receiver._load()


def test_replay_mlat_marker_clock_does_not_stall_playback(tmp_path):
    frames = _replay_frames(
        tmp_path,
        "@0000000000008DABCDEF000000;\n"
        "@FF004D4C41548DABCDEF000001;\n"
        "@000000B71B008DABCDEF000002;\n",
    )
    stamps = [frame.stamp for frame in frames]
    assert len(stamps) == 3
    assert max(stamps) < 5.0
    assert stamps[2] - stamps[0] < 2.0


def test_replay_backwards_clock_does_not_stall_playback(tmp_path):
    # Receiver restart mid-capture: the clock jumps back from 1 s to 0.
    frames = _replay_frames(
        tmp_path,
        "@000000B71B008DABCDEF000000;\n"
        "@0000000000008DABCDEF000001;\n"
        "@0000005B8D808DABCDEF000002;\n",
    )
    stamps = [frame.stamp for frame in frames]
    assert max(stamps) < 5.0
    assert stamps == sorted(stamps)
    # Spacing after the discontinuity still follows the clock (0.5 s).
    assert abs((stamps[2] - stamps[1]) - 0.5) < 1e-6


def test_replay_clock_wraparound_keeps_spacing(tmp_path):
    frames = _replay_frames(
        tmp_path,
        "@FFFFFFA472808DABCDEF000000;\n"  # 0.5 s before the 48-bit wrap
        "@0000005B8D808DABCDEF000001;\n",  # 0.5 s after it
    )
    stamps = [frame.stamp for frame in frames]
    assert abs((stamps[1] - stamps[0]) - 1.0) < 0.01


# --- Replay carries the receiver clock and signal level ----------------------


def _replay_emitted(tmp_path, name: str, data: bytes) -> list[tuple]:
    path = tmp_path / name
    path.write_bytes(data)
    emitted: list[tuple] = []
    receiver = ReplayReceiver(
        "replay",
        lambda hex_msg, ts, **kw: emitted.append((hex_msg, ts, kw)),
        {"path": str(path), "loop": False, "speed": 1000.0},
    )
    thread = threading.Thread(target=receiver.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and len(emitted) < 3:
        time.sleep(0.01)
    receiver.stop()
    thread.join(timeout=2.0)
    return emitted


def test_replay_passes_avr_clock_to_emit(tmp_path):
    emitted = _replay_emitted(
        tmp_path,
        "avr.txt",
        b"@0000000000008DABCDEF000000;\n"
        b"@FF004D4C41548DABCDEF000001;\n"
        b"@000000B71B008DABCDEF000002;\n",
    )
    assert [kw.get("clock") for _h, _t, kw in emitted] == [
        0,
        BEAST_MLAT_CLOCK,  # kept so integrity can tell MLAT results apart
        12_000_000,
    ]
    assert all(kw.get("rssi") is None for _h, _t, kw in emitted)
    # Engine receive time, not capture time.
    assert all(abs(ts - time.time()) < 5 for _h, ts, _kw in emitted)


def _beast(hex_msg: str, clock: int, signal: int) -> bytes:
    body = clock.to_bytes(6, "big") + bytes([signal]) + bytes.fromhex(hex_msg)
    escaped = bytearray()
    for byte in body:
        escaped.append(byte)
        if byte == BEAST_ESC:
            escaped.append(BEAST_ESC)
    kind = {2: 0x31, 7: 0x32, 14: 0x33}[len(body) - 7]
    return bytes([BEAST_ESC, kind]) + bytes(escaped)


def test_replay_reads_beast_binary_captures(tmp_path):
    capture = (
        _beast(IDENT, 0, 200)
        + _beast("8d1a6b902015a678d4d220aa4bda", 6_000_000, 0x1A)
        + _beast("5d484fdea248f5", 12_000_000, 100)
    )
    path = tmp_path / "capture.bin"
    path.write_bytes(capture)
    receiver = ReplayReceiver("replay", lambda *_a, **_k: None, {"path": str(path)})
    frames = receiver._load()
    assert [f.hex for f in frames] == [
        IDENT,
        "8d1a6b902015a678d4d220aa4bda",
        "5d484fdea248f5",
    ]
    assert [f.stamp for f in frames] == pytest.approx([0.0, 0.5, 1.0])
    assert [f.clock for f in frames] == [0, 6_000_000, 12_000_000]
    assert frames[0].rssi == pytest.approx(beast_rssi_dbfs(200))
    assert frames[1].rssi == pytest.approx(beast_rssi_dbfs(0x1A))

    emitted = _replay_emitted(tmp_path, "capture.bin", capture)
    assert [kw["clock"] for _h, _t, kw in emitted] == [0, 6_000_000, 12_000_000]
    assert emitted[0][2]["rssi"] == pytest.approx(beast_rssi_dbfs(200))


# --- The shipped offline replay example and replay speed ---------------------


EXAMPLES = Path(pyaerial.__file__).parent / "examples"


def test_replay_example_ships_its_capture():
    config = load_config(EXAMPLES / "replay.yaml")
    options = config.receivers["recorded"].receiver_arguments()
    path = Path(options["path"])
    assert path.is_file()
    assert path.stat().st_size < 300_000
    assert float(options["speed"]) == 1.0


def test_replay_example_tracks_every_aircraft_as_genuine(tmp_path, monkeypatch):
    """The shipped capture, through the engine tick on a simulated clock:
    every aircraft is tracked and none is flagged by the integrity checks."""
    import pyaerial.enrich.aircraft_db as aircraft_db
    from pyaerial.engine import Engine

    def offline(*_args, **_kwargs):
        raise requests.ConnectionError("offline test")

    monkeypatch.setattr(aircraft_db.requests, "get", offline)
    config = load_config(EXAMPLES / "replay.yaml")
    receiver = ReplayReceiver(
        "recorded",
        lambda *_a, **_k: None,
        config.receivers["recorded"].receiver_arguments(),
    )
    frames = receiver._load()
    start = time.time()
    clock = {"now": start}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    engine = Engine(config, isolated=True, aircraft_db_path=str(tmp_path / "a.db"))
    flagged: dict[str, set[str]] = {}
    try:
        index = 0
        while clock["now"] < start + frames[-1].stamp + 1.0:
            clock["now"] += 0.5
            while index < len(frames) and start + frames[index].stamp <= clock["now"]:
                frame = frames[index]
                index += 1
                engine._enqueue_message(
                    frame.hex, start + frame.stamp, "recorded", clock=frame.clock
                )
            engine._tick()
            for icao, plane in engine.tracker.planes.items():
                status = (plane.info.get("integrity") or {}).get("status")
                if status in ("suspect", "spoofed"):
                    flagged.setdefault(icao, set()).add(status)
        planes = dict(engine.tracker.planes)
    finally:
        engine.shutdown()
    assert len(planes) == 6
    assert flagged == {}
    for plane in planes.values():
        integrity = plane.info["integrity"]
        assert integrity["status"] == "ok"
        # The checks that need a clock and kinematics actually ran.
        assert {"timing", "kinematics", "message_mix"} <= set(integrity["checks"])


def test_replay_speed_other_than_one_warns(tmp_path, caplog):
    path = tmp_path / "raw.txt"
    path.write_text(IDENT + "\n")
    with caplog.at_level(logging.WARNING, logger="pyaerial.receiver.replay"):
        ReplayReceiver(
            "replay", lambda *_a, **_k: None, {"path": str(path), "speed": 4}
        )
    assert any("speed 4" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="pyaerial.receiver.replay"):
        ReplayReceiver(
            "replay", lambda *_a, **_k: None, {"path": str(path), "speed": 1}
        )
    assert not caplog.records
