from __future__ import annotations

import threading

from pyaerial.receivers.replay import ReplayReceiver


def test_replay_loads_timestamped_and_bare_hex(tmp_path):
    path = tmp_path / "raw.txt"
    path.write_text("# comment\n1.0 AABBCC\nDDEEFF\n")
    receiver = ReplayReceiver("replay", lambda *_args: None, {"path": str(path), "loop": False})
    frames = receiver._load()
    assert len(frames) == 2
    assert frames[0][1] == "aabbcc"
    assert frames[1][1] == "ddeeff"


def test_replay_loads_avr_star_and_clock_lines(tmp_path):
    path = tmp_path / "avr.txt"
    path.write_text("*8DABCDEF000000;\n@0000000000018DABCDEF000001;\n")
    receiver = ReplayReceiver("replay", lambda *_args: None, {"path": str(path), "loop": False})
    frames = receiver._load()
    assert len(frames) == 2
    assert frames[0][1].startswith("8dabcdef")
    assert frames[1][1].startswith("8dabcdef")


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
        lambda hex_msg, _ts: emitted.append(hex_msg),
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
