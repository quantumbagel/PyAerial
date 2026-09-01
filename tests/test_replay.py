from __future__ import annotations

from pyaerial.receivers.replay import ReplayReceiver


def test_replay_loads_timestamped_and_bare_hex(tmp_path):
    path = tmp_path / "raw.txt"
    path.write_text("# comment\n1.0 AABBCC\nDDEEFF\n")
    receiver = ReplayReceiver("replay", lambda *_args: None, {"path": str(path), "loop": False})
    frames = receiver._load()
    assert len(frames) == 2
    assert frames[0][1] == "AABBCC"
    assert frames[1][1] == "DDEEFF"


def test_replay_missing_file():
    receiver = ReplayReceiver(
        "replay", lambda *_args: None, {"path": "/no/such/file.txt", "loop": False}
    )
    reason = receiver.run()
    assert reason and "not found" in reason
