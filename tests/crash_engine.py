"""A tracking engine for the crash-injection test (``test_crash_injection.py``).

Runs a real :class:`~pyaerial.engine.Engine` over a SQLite archive (no
Redis: ``NullLiveWriter``) fed with synthetic aircraft that fly back and
forth across the ``pad`` zone, so alerts activate and clear every few
seconds. Each alert notification is appended to a capture file *before*
the episode is recorded (the engine's order), as a webhook consumer would
see it. SIGTERM stops it gracefully; the test kills it with SIGKILL.

The checkpoint constants are scaled down (1 s, 2 s settle) so that runs of
a few seconds exercise checkpoints and recovery.

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
from pyaerial.config.schema import (  # noqa: E402
    AlertActionConfig,
    TrackingConfig,
    ZoneConfig,
)
from pyaerial.engine import Engine  # noqa: E402
from pyaerial.engine import archiver as archiver_mod  # noqa: E402
from pyaerial.receivers.frames import RawFrame  # noqa: E402
from pyaerial.store import ArchiveWriter  # noqa: E402

archiver_mod.CHECKPOINT_SECONDS = 1.0
archiver_mod.SETTLE_SECONDS = 2.0

HZ = 10.0
AIRCRAFT = 8
SPEED_MPS = 150.0
WEST, EAST, LAT = -78.705, -78.685, 35.725  # the pad spans -78.70 .. -78.69
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


def _position(n: int, now: float) -> tuple[float, float]:
    """Longitude and track of aircraft *n*: back and forth across the pad."""
    span = (EAST - WEST) * 111_320 * math.cos(math.radians(LAT))
    leg = span / SPEED_MPS
    phase = (now / leg + n / AIRCRAFT * 2) % 2.0
    if phase < 1.0:
        return WEST + (EAST - WEST) * phase, 90.0
    return EAST - (EAST - WEST) * (phase - 1.0), 270.0


def main() -> int:
    global _CAPTURE
    archive, capture, ready = sys.argv[1:4]
    _CAPTURE = Path(capture)
    engine = Engine(
        _config(archive),
        isolated=True,
        aircraft_db_path=archive + ".aircraft",
        history_store=ArchiveWriter(archive),
    )
    # Notify inline, as the dispatcher thread would just before the record.
    engine.calculator.alerts._dispatcher.close(timeout=0.0)
    engine.calculator.alerts._dispatcher = InlineDispatcher()  # type: ignore[assignment]
    signal.signal(signal.SIGTERM, lambda *_: engine.request_stop())
    with open(ready, "a") as out:
        out.write(f"{time.time()}\n")
    odd = 0
    tick = 0
    try:
        while not engine.stop_requested:
            now = time.time()
            stamp = now - 0.2  # older than the supervisor's merge window
            odd ^= 1
            for n in range(AIRCRAFT):
                icao = f"c0ffe{n}"
                lon, track = _position(n, now)
                frames = [position_frame(icao, LAT, lon, 300.0, odd)]
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
