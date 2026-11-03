"""Replay receiver streaming recorded Mode S frames from capture files
(AVR / hex text, or dump1090 Beast binary)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, NamedTuple

from pyaerial.receivers import Receiver, register_receiver
from pyaerial.receivers.frames import (
    BEAST_CLOCK_HZ,
    BEAST_CLOCK_MOD,
    BEAST_ESC,
    BEAST_MLAT_CLOCK,
    BEAST_MSG_LEN,
    BeastParser,
    parse_avr_line,
)

# A forward clock step longer than this is a discontinuity (receiver restart,
# merged captures), not a quiet sky: a backwards step wraps to ~271 days.
_MAX_CLOCK_GAP_SECONDS = 3600.0


class ReplayFrame(NamedTuple):
    """One recorded frame: playback offset (s), hex, 12 MHz clock, RSSI (dBFS)."""

    stamp: float
    hex: str
    clock: int | None = None
    rssi: float | None = None


def _as_bool(value: object, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"false", "0", "no", "off"}:
            return False
        if lowered in {"true", "1", "yes", "on"}:
            return True
    return default


@register_receiver("replay")
class ReplayReceiver(Receiver):
    """Ingest Mode S hex frames from disk with optional timestamp pacing."""

    known_options = frozenset({"path", "speed", "loop", "interval"})

    @classmethod
    def validate_options(cls, options: dict[str, Any], *, config_dir: Path) -> list[str]:
        raw_path = options.get("path")
        if not raw_path:
            return ["replay requires options.path"]
        path = Path(str(raw_path)).expanduser()
        if not path.is_absolute():
            path = config_dir / path
        path = path.resolve()
        # Normalized in place: the engine may run from another directory.
        options["path"] = str(path)
        if not path.is_file():
            return [f"options.path: not found: {path}"]
        return []

    def configure(self, arguments: dict) -> None:
        path = arguments.get("path")
        if not path:
            raise ValueError("replay receiver requires options.path")
        self.path = Path(str(path))
        try:
            self.speed = float(arguments.get("speed", 1.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("replay options.speed must be a number") from exc
        if self.speed <= 0:
            raise ValueError("replay options.speed must be greater than 0")
        if self.speed != 1.0:
            # Frames are stamped with replay (wall) time: expiry, de-duplication
            # and the live store all run on wall time, so capture-rate stamps
            # would drift into the future or past. Speed therefore scales time.
            self.log.warning(
                "Replay speed %g: aircraft will appear to move %g times faster "
                "than they report, and the kinematics and timing integrity "
                "checks will flag them. Use speed 1.0 for integrity results.",
                self.speed,
                self.speed,
            )
        self.loop = _as_bool(arguments.get("loop", True), default=True)
        try:
            self.interval = float(arguments.get("interval", 0.1))
        except (TypeError, ValueError) as exc:
            raise ValueError("replay options.interval must be a number") from exc
        if self.interval < 0:
            raise ValueError("replay options.interval must be >= 0")

    def run(self) -> str | None:
        if not self.path.is_file():
            return f"replay file not found: {self.path}"
        frames = self._load()
        if not frames:
            return f"replay file empty: {self.path}"
        while not self.should_stop():
            t0 = frames[0].stamp
            wall0 = time.time()
            for frame in frames:
                delay = max(
                    0.0, ((frame.stamp - t0) / self.speed) - (time.time() - wall0)
                )
                if delay > 0 and self._stop.wait(delay):
                    return None
                # Engine receive time, like a live feed. The capture's clock
                # and signal level pass through untouched for the integrity
                # checks (timing, TDOA, signal strength).
                self.emit(frame.hex, time.time(), rssi=frame.rssi, clock=frame.clock)
            if not self.loop:
                # Stay alive so the engine does not restart (and re-play) it.
                self.log.info("Replay of %s finished", self.path)
                self._stop.wait()
                return None
        return None

    def _load(self) -> list[ReplayFrame]:
        data = self.path.read_bytes()
        rows: list[tuple[float | None, str, int | None, float | None]]
        if _is_beast(data):
            rows = [
                (None, hex_msg, clock, rssi)
                for hex_msg, rssi, clock in BeastParser().feed(data)
            ]
        else:
            rows = _text_rows(data.decode("utf-8", errors="ignore"))
        frames: list[ReplayFrame] = []
        sequential = 0.0
        last_clock: int | None = None
        last_clock_stamp = 0.0
        for given, hex_msg, clock, rssi in rows:
            stamp = given
            # A synthetic MLAT result's clock is a marker, not a time; it is
            # still emitted so the integrity checks can recognise it.
            pace_clock = None if clock == BEAST_MLAT_CLOCK else clock
            if stamp is None and pace_clock is not None:
                # Pace by the step from the previous clock (not the first)
                # so 48-bit wraps accumulate and a discontinuity costs one
                # interval instead of stalling playback for months.
                if last_clock is None:
                    stamp = sequential
                else:
                    delta = (
                        (pace_clock - last_clock) % BEAST_CLOCK_MOD
                    ) / BEAST_CLOCK_HZ
                    if delta > _MAX_CLOCK_GAP_SECONDS:
                        delta = self.interval
                    stamp = last_clock_stamp + delta
                last_clock = pace_clock
                last_clock_stamp = stamp
            elif stamp is None:
                stamp = sequential
                sequential += self.interval
            sequential = max(sequential, stamp)
            frames.append(ReplayFrame(stamp, hex_msg, clock, rssi))
        return frames


def _is_beast(data: bytes) -> bool:
    """Whether a capture is dump1090 Beast binary (port 30005) rather than text."""
    return len(data) >= 2 and data[0] == BEAST_ESC and data[1] in BEAST_MSG_LEN


def _text_rows(
    text: str,
) -> list[tuple[float | None, str, int | None, float | None]]:
    """``(stamp, hex, clock, rssi)`` from ``*HEX;``, ``@CLOCKHEX;``, bare hex
    or ``TIMESTAMP HEX`` lines; *stamp* is ``None`` unless the line has one."""
    rows: list[tuple[float | None, str, int | None, float | None]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        stamp: float | None = None
        parsed: tuple[str, int | None] | None = None
        if len(parts) >= 2:
            try:
                stamp = float(parts[0])
            except ValueError:
                stamp = None
            else:
                parsed = parse_avr_line(parts[1], strict=False)
        if stamp is None:
            parsed = parse_avr_line(line, strict=False)
        if not parsed:
            continue
        hex_msg, clock = parsed
        rows.append((stamp, hex_msg, clock, None))
    return rows
