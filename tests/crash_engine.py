"""A tracking engine for the crash-injection test (``test_crash_injection.py``).

Runs a real :class:`~pyaerial.engine.Engine` over a SQLite archive (no
Redis: ``NullLiveWriter``) fed with synthetic aircraft that fly laps across
the ``pad`` zone, so alerts activate and clear every few seconds. Each alert notification is appended to a capture file *before*
the episode is recorded (the engine's order), as a webhook consumer would
see it. SIGTERM stops it gracefully; the test kills it with SIGKILL.

The checkpoint constants are scaled down (1 s, 2 s settle) so that runs of
a few seconds exercise checkpoints and recovery. A loop iteration that
stalls (scheduling, a slow disk flush) leaves a real gap in the samples; it
is appended to ``READY_FILE.stalls`` as ``start end`` so the test can tell
it from a sample the archive lost.

Usage: ``python tests/crash_engine.py ARCHIVE CAPTURE READY_FILE``
"""

from __future__ import annotations

import json
import math
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from e2e_support import position_frame, velocity_frame  # noqa: E402
from helpers import InlineDispatcher, make_config, make_rule  # noqa: E402
from pyaerial.alerters import Alerter, register_alerter  # noqa: E402
from pyaerial.alerts import engine as alerts_engine_mod  # noqa: E402
from pyaerial.config.schema import (  # noqa: E402
    AlertActionConfig,
    TrackingConfig,
    ZoneConfig,
)
from pyaerial.engine import Engine  # noqa: E402
from pyaerial.engine import archiver as archiver_mod  # noqa: E402
from pyaerial.receivers.frames import RawFrame  # noqa: E402
from pyaerial.store.archive.writer import ArchiveWriter  # noqa: E402
from pyaerial.store.archive import writer as archive_writer  # noqa: E402

archiver_mod.CHECKPOINT_SECONDS = 1.0
archiver_mod.SETTLE_SECONDS = 2.0
# A killed engine's archive lease expires after this (the real 10 s would make
# every restart wait).
archive_writer.LEASE_TTL_SECONDS = 1.0

HZ = 10.0
STALL_SECONDS = 0.5
AIRCRAFT = 8
SPEED_MPS = 150.0
WEST, EAST, LAT = -78.705, -78.685, 35.725  # the pad spans -78.70 .. -78.69
# Laps are a stadium: legs TURN_RADIUS_M either side of LAT, joined by
# semicircles beyond the pad. Reversing along one line retraced the outbound
# positions, and a frame byte-identical to one sent within
# tracking.duplicate_packet_merging (5 s) is dropped as a duplicate, by
# design: up to ~2.5 s of samples around each turn were never tracked.
TURN_RADIUS_M = 100.0
_M_PER_DEG = 111_320.0
_CAPTURE: Path | None = None


@register_alerter("crash_capture")
class CaptureAlerter(Alerter):
    known_options = frozenset()

    def alert(self, meta: dict, payload: dict) -> None:
        reason = meta.get("reason") or {}
        line = json.dumps(
            {
                "hook": reason.get("hook"),
                "alert_id": meta.get("alert_id"),
                "ended_by": reason.get("ended_by"),
            }
        )
        assert _CAPTURE is not None
        with _CAPTURE.open("a") as out:
            out.write(line + "\n")


def _config(archive: str):
    capture = [AlertActionConfig(method="crash_capture")]
    rule = make_rule(
        name="inside",
        retain=True,
        dwell_seconds=1,
        altitude={"max": 2000},
        eta={"max": 1},
    )
    rule.on_activate = capture
    rule.on_deactivate = capture
    pad = ZoneConfig(
        coordinates=[
            [35.72, -78.70],
            [35.73, -78.70],
            [35.73, -78.69],
            [35.72, -78.69],
            [35.72, -78.70],
        ],
        rules=[rule],
    )
    config = make_config(zones={"pad": pad}, tracking=TrackingConfig(hz=HZ))
    config.database.path = archive
    config.enrichment.enabled = False
    return config


def _position(n: int, now: float) -> tuple[float, float, float]:
    """Latitude, longitude and track of aircraft *n* on its lap."""
    cos_lat = math.cos(math.radians(LAT))
    leg = (EAST - WEST) * _M_PER_DEG * cos_lat
    arc = math.pi * TURN_RADIUS_M
    lap = 2 * (leg + arc)
    s = (now * SPEED_MPS + n / AIRCRAFT * lap) % lap
    r_lat = TURN_RADIUS_M / _M_PER_DEG
    r_lon = r_lat / cos_lat
    if s < leg:  # eastbound
        return LAT - r_lat, WEST + (EAST - WEST) * s / leg, 90.0
    s -= leg
    if s < arc:  # turning north around the east end
        a = s / TURN_RADIUS_M
        return (
            LAT - r_lat * math.cos(a),
            EAST + r_lon * math.sin(a),
            (90.0 - math.degrees(a)) % 360,
        )
    s -= arc
    if s < leg:  # westbound
        return LAT + r_lat, EAST - (EAST - WEST) * s / leg, 270.0
    a = (s - leg) / TURN_RADIUS_M  # turning south around the west end
    return (
        LAT + r_lat * math.cos(a),
        WEST - r_lon * math.sin(a),
        (270.0 - math.degrees(a)) % 360,
    )


def main() -> int:
    global _CAPTURE
    archive, capture, ready = sys.argv[1:4]
    _CAPTURE = Path(capture)
    # Notify inline, as the dispatcher thread would just before the record.
    # Installed before the engine is built: its startup recovery already
    # sends restart deactivates, which a real dispatcher closed right after
    # construction could discard before its worker ran them.
    alerts_engine_mod.AlertDispatcher = InlineDispatcher  # type: ignore[assignment,misc]
    engine = Engine(
        _config(archive),
        isolated=True,
        aircraft_db_path=archive + ".aircraft",
        history_store=ArchiveWriter(archive),
    )
    signal.signal(signal.SIGTERM, lambda *_: engine.request_stop())
    with open(ready, "a") as out:
        out.write(f"{time.time()}\n")
    odd = 0
    tick = 0
    last: float | None = None
    stalls = Path(ready + ".stalls")
    try:
        while not engine.stop_requested:
            now = time.time()
            if last is not None and now - last > STALL_SECONDS:
                with stalls.open("a") as out:
                    out.write(f"{last - 0.2} {now - 0.2}\n")  # frame stamps
            last = now
            stamp = now - 0.2  # stamped before it is queued, like dump1090
            odd ^= 1
            for n in range(AIRCRAFT):
                icao = f"c0ffe{n}"
                lat, lon, track = _position(n, now)
                frames = [position_frame(icao, lat, lon, 300.0, odd)]
                if tick % 5 == 0:
                    frames.append(velocity_frame(icao, SPEED_MPS * 3.6, track))
                for hex_msg in frames:
                    engine.supervisor.enqueue(RawFrame(hex_msg, stamp, "main"))
            engine.tick()
            tick += 1
            time.sleep(max(0.0, 1.0 / HZ - (time.time() - now)))
    finally:
        engine.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
