"""End to end: AVR capture file -> replay receiver -> Engine -> SQLite archive."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from e2e_support import (
    PYAERIAL_ENV,
    EngineThread,
    count,
    ident_frame,
    install_fake_external_http,
    install_fake_redis,
    position_frame,
    quantized_alt_ft,
    query,
    velocity_frame,
    wait_for,
    write_config,
)
from pyaerial.config import load_config
from pyaerial.engine import Engine
from pyaerial.integrity import geodesy

HOME = (35.7275, -78.6959)
ICAO = "a0c0de"
CALLSIGN = "E2ED04"
GHOST = "a0c0df"  # address a corrupted frame would claim
SPEED_KMH = 500.0
HEADING = 90.0
ALT_M = 5000.0
POSITIONS = 20
SPACING = 0.5  # capture seconds between position squitters
PLAYBACK_SPEED = 10.0
START = geodesy.destination(*HOME, 270, 40_000)
CAPTURE_T0 = 1_700_000_000.0


def _capture(path: Path) -> tuple[list[str], dict[str, tuple[float, float, float]]]:
    """Write a capture mixing the text forms replay accepts; return frames + truth."""
    lines = [
        "# PyAerial e2e capture: TIMESTAMP *HEX; / TIMESTAMP @CLOCKHEX; / TIMESTAMP HEX"
    ]
    frames: list[str] = []
    truth: dict[str, tuple[float, float, float]] = {}
    ident = ident_frame(ICAO, CALLSIGN)
    lines.append(f"{CAPTURE_T0:.3f} *{ident};")
    frames.append(ident)
    for i in range(POSITIONS):
        t = CAPTURE_T0 + 0.1 + i * SPACING
        lat, lon = geodesy.destination(
            *START, HEADING, SPEED_KMH / 3.6 * (t - CAPTURE_T0)
        )
        odd = i % 2
        hex_msg = position_frame(ICAO, lat, lon, ALT_M, odd)
        truth[hex_msg] = (lat, lon, quantized_alt_ft(ALT_M) * 0.3048)
        frames.append(hex_msg)
        if odd:
            clock = int((t - CAPTURE_T0) * 12e6)
            lines.append(f"{t:.3f} @{clock:012x}{hex_msg};")
        else:
            lines.append(f"{t:.3f} *{hex_msg};")
        velocity = velocity_frame(ICAO, SPEED_KMH, HEADING)
        lines.append(f"{t + 0.1:.3f} {velocity}")
        frames.append(velocity)
        if i == 7:
            # One bit error in the address: parity fails, so it must not
            # create a ghost aircraft (but the raw archive keeps it).
            corrupt = hex_msg[:7] + f"{int(hex_msg[7], 16) ^ 1:x}" + hex_msg[8:]
            assert corrupt[2:8] == GHOST
            lines.append(f"{t + 0.2:.3f} *{corrupt};")
            frames.append(corrupt)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return frames, truth


@pytest.fixture
def replay_run(tmp_path, monkeypatch):
    for name in PYAERIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    install_fake_redis(monkeypatch)
    external = install_fake_external_http(monkeypatch)
    frames, truth = _capture(tmp_path / "capture.txt")
    config_path = write_config(
        tmp_path / "config.yaml",
        {
            "database": {
                "path": "replay.db",
                "redis_uri": "redis://e2e-fake-redis:6379/0",
            },
            "tracking": {"hz": 20},
            "archive": {"raw_frames": True},
            "home": {"latitude": HOME[0], "longitude": HOME[1]},
            "web": {"beast_port": None},
            "receivers": {
                "capture": {
                    "type": "replay",
                    "options": {
                        "path": "capture.txt",
                        "speed": PLAYBACK_SPEED,
                        "loop": False,
                    },
                    "position": {"latitude": HOME[0], "longitude": HOME[1]},
                }
            },
        },
    )
    # Replay paths are relative to the working directory, like the CLI.
    monkeypatch.chdir(tmp_path)
    config = load_config(config_path)
    db = Path(config.database.path)
    engine = Engine(config, aircraft_db_path=str(tmp_path / "aircraft.db"))
    runner = EngineThread(engine).start()
    try:
        wait_for(
            lambda: count(db, "SELECT COUNT(*) FROM frames") >= len(frames),
            15,
            message="replayed frames in the raw archive",
        )
    finally:
        runner.stop()
    assert not external.blocked
    return db, frames, truth


def test_replay_capture_is_archived_end_to_end(replay_run):
    db, frames, truth = replay_run
    flights = query(db, "SELECT * FROM flights")
    # One aircraft; the corrupted frame did not create a ghost.
    assert [f["icao"] for f in flights] == [ICAO]
    (flight,) = flights
    assert flight["callsign"] == CALLSIGN

    rows = query(
        db,
        "SELECT * FROM telemetry WHERE flight_id = ? ORDER BY timestamp",
        (flight["flight_id"],),
    )
    # Every position, except the first (its even/odd partner comes next).
    assert len(rows) == POSITIONS - 1
    for row in rows:
        (frame,) = query(
            db,
            "SELECT hex FROM frames WHERE timestamp = ? AND icao = ?",
            (row["timestamp"], ICAO),
        )
        lat, lon, alt = truth[frame["hex"]]
        assert row["latitude"] == pytest.approx(lat, abs=1e-4)
        assert row["longitude"] == pytest.approx(lon, abs=1e-4)
        assert row["altitude"] == pytest.approx(alt, abs=0.5)
        assert row["speed"] == pytest.approx(SPEED_KMH, abs=2.0)
        assert row["heading"] == pytest.approx(HEADING, abs=1.0)
        assert json.loads(row["receivers"]) == ["capture"]
    # Paced by the capture's timestamps at 10x: ~1 s, not dumped at once.
    span = rows[-1]["timestamp"] - rows[0]["timestamp"]
    capture_span = (POSITIONS - 2) * SPACING
    assert (
        capture_span / PLAYBACK_SPEED * 0.5 < span < capture_span / PLAYBACK_SPEED * 4
    )


def test_replay_raw_archive_keeps_every_line(replay_run):
    db, frames, _truth = replay_run
    archived = query(
        db, "SELECT receiver, hex, df, icao, clock FROM frames ORDER BY timestamp"
    )
    assert sorted(r["hex"] for r in archived) == sorted(frames)
    assert {r["receiver"] for r in archived} == {"capture"}
    assert {r["df"] for r in archived} == {17}
    # @-lines carry the 12 MHz clock; *-lines have none.
    with_clock = [r for r in archived if r["clock"] is not None]
    assert len(with_clock) == POSITIONS // 2
    assert all(int(r["hex"][8:22], 16) >> 34 & 1 for r in with_clock)  # the odd ones
    ghost = [r for r in archived if r["icao"] == GHOST]
    assert len(ghost) == 1
