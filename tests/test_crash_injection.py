"""Crash injection: an engine (``tests/crash_engine.py``) over one archive is
killed with SIGKILL at random points, restarted, and finally stopped
gracefully; the archive and the alert notifications must stay consistent.

Invariants checked at the end:

- nothing is left ``live`` or active, and every alert row points at a flight;
- every recorded episode was notified once on activation and closed with
  one deactivate (a kill between a notification and its record may repeat
  one restart deactivate, or leave one activation that was never recorded;
  both are bounded by the number of kills);
- an interrupted flight has no gap in its samples, and lost at most the
  checkpoint window before the kill;
- flights of one aircraft never overlap.

Slow (about a minute): ``pytest -m slow``; CI runs it as its own step.
"""

from __future__ import annotations

import json
import random
import signal
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals"),
]

DRIVER = Path(__file__).with_name("crash_engine.py")
KILLS = 10
# Scaled-down CHECKPOINT_SECONDS + SETTLE_SECONDS in the driver (1 s + 2 s),
# plus a tick, the checkpoint slice spread, and scheduling slack.
LOSS_BOUND = 1.0 + 2.0 + 0.1 + 1.0 + 1.0
# A run this long (after startup) has checkpointed settled samples of every
# aircraft, so its interrupted flights can be checked against LOSS_BOUND.
SETTLED_RUN = 4.0
MAX_SAMPLE_GAP = 1.0  # positions every 0.1 s


def _spawn(tmp_path: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            str(DRIVER),
            str(tmp_path / "pyaerial.db"),
            str(tmp_path / "capture.jsonl"),
            str(tmp_path / "ready"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _ready_times(tmp_path: Path) -> list[float]:
    path = tmp_path / "ready"
    return [float(line) for line in path.read_text().split()] if path.exists() else []


def _wait_ready(tmp_path: Path, count: int, timeout: float = 20.0) -> float:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        times = _ready_times(tmp_path)
        if len(times) >= count:
            return times[count - 1]
        time.sleep(0.05)
    raise AssertionError("crash engine did not start")


def test_kill_9_loop_keeps_the_archive_and_notifications_consistent(tmp_path):
    rng = random.Random(20261103)
    runs: list[tuple[float | None, float]] = []  # (ready time, kill time)
    for _ in range(KILLS):
        before = len(_ready_times(tmp_path))
        proc = _spawn(tmp_path)
        # Some kills land during startup recovery, most while tracking.
        time.sleep(rng.uniform(1.0, 7.0))
        killed_at = time.time()
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        times = _ready_times(tmp_path)
        runs.append((times[before] if len(times) > before else None, killed_at))
    final = _spawn(tmp_path)
    _wait_ready(tmp_path, len(_ready_times(tmp_path)) + 1)
    time.sleep(3.0)
    final.send_signal(signal.SIGTERM)
    assert final.wait(timeout=30) == 0

    conn = sqlite3.connect(tmp_path / "pyaerial.db")
    flights = conn.execute(
        "SELECT flight_id, icao, status, start_time, end_time FROM flights"
    ).fetchall()
    alerts = conn.execute("SELECT alert_id, flight_id, active FROM alerts").fetchall()
    orphans = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE flight_id NOT IN "
        "(SELECT flight_id FROM flights)"
    ).fetchone()[0]
    samples = defaultdict(list)
    for flight_id, stamp in conn.execute(
        "SELECT flight_id, timestamp FROM telemetry ORDER BY flight_id, timestamp"
    ):
        samples[flight_id].append(stamp)
    conn.close()
    events = [
        json.loads(line)
        for line in (tmp_path / "capture.jsonl").read_text().splitlines()
    ]

    # Nothing left open; every alert row has its flight.
    assert flights and alerts
    assert {status for _f, _i, status, _s, _e in flights} <= {
        "completed",
        "interrupted",
    }
    assert all(active == 0 for _a, _f, active in alerts)
    assert orphans == 0

    # Exactly-once pairing, up to the documented kill windows.
    activates = Counter(e["alert_id"] for e in events if e["hook"] == "activate")
    deactivates = Counter(e["alert_id"] for e in events if e["hook"] == "deactivate")
    recorded = {alert_id for alert_id, _f, _a in alerts}
    assert set(deactivates) <= set(activates)
    assert all(activates[alert_id] == 1 for alert_id in recorded)
    assert all(1 <= deactivates[alert_id] <= 2 for alert_id in recorded)
    repeated = sum(deactivates[alert_id] - 1 for alert_id in recorded)
    unrecorded = set(activates) - recorded
    assert repeated + len(unrecorded) <= KILLS
    assert any(
        e["ended_by"] == "restart" for e in events if e["hook"] == "deactivate"
    ), "no kill left an open episode to close at restart"

    # Interrupted flights: no gaps, and at most the checkpoint window lost.
    interrupted = [f for f in flights if f[2] == "interrupted"]
    assert interrupted
    alerted = {flight_id for _a, flight_id, _active in alerts}
    for flight_id, *_rest in interrupted:
        stamps = samples[flight_id]
        if not stamps:
            # Killed before a sample settled: kept only for its retain alert.
            assert flight_id in alerted, flight_id
            continue
        gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
        assert max(gaps, default=0.0) <= MAX_SAMPLE_GAP, flight_id
    checked = 0
    for ready, killed_at in runs:
        if ready is None or killed_at - ready < SETTLED_RUN:
            continue  # killed before anything could settle
        theirs = [f for f in interrupted if ready - 1 <= f[3] <= killed_at]
        assert len({f[1] for f in theirs}) == 8, (ready, killed_at)
        for flight_id, _icao, _status, _start, end in theirs:
            assert killed_at - end <= LOSS_BOUND, (flight_id, killed_at - end)
        checked += 1
    assert checked, "no run lived long enough to check the loss bound"

    print(
        f"crash injection: {KILLS} kills, {len(flights)} flights "
        f"({len(interrupted)} interrupted), {len(recorded)} episodes, "
        f"{sum(1 for e in events if e['ended_by'] == 'restart')} restart "
        f"deactivates, {repeated} repeated, {len(unrecorded)} unrecorded "
        f"activations, loss bound checked on {checked} runs"
    )

    # One aircraft is never tracked as two flights at once.
    by_icao = defaultdict(list)
    for _flight_id, icao, _status, start, end in flights:
        by_icao[icao].append((start, end))
    for spans in by_icao.values():
        spans.sort()
        for (_s1, end1), (start2, _e2) in zip(spans, spans[1:], strict=False):
            assert start2 >= end1 - 1e-6
