"""Replay a recorded dump1090-style raw hex file."""

from __future__ import annotations

import time
from pathlib import Path

from pyaerial.receivers import Receiver, register_receiver
from pyaerial.receivers.frames import parse_avr_line


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
    """Play hex frames from a text file (optional ``timestamp hex`` per line)."""

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
        self.loop = _as_bool(arguments.get("loop", True), default=True)
        try:
            self.interval = float(arguments.get("interval", 0.0))
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
            t0 = frames[0][0]
            wall0 = time.time()
            for stamp, hex_msg in frames:
                delay = max(0.0, ((stamp - t0) / self.speed) - (time.time() - wall0))
                if delay > 0 and self._stop.wait(delay):
                    return None
                self.emit(hex_msg, time.time())
            if not self.loop:
                self._stop.wait()
                return "replay finished"
        return None

    def _load(self) -> list[tuple[float, str]]:
        frames: list[tuple[float, str]] = []
        sequential = 0.0
        for raw in self.path.read_text(errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            stamp: float | None = None
            hex_msg = ""
            if len(parts) >= 2:
                try:
                    stamp = float(parts[0])
                    parsed = parse_avr_line(parts[1])
                    hex_msg = parsed[0] if parsed else parts[1]
                except ValueError:
                    stamp = None
            if stamp is None:
                parsed = parse_avr_line(line)
                if not parsed:
                    continue
                hex_msg = parsed[0]
                stamp = sequential
                sequential += self.interval
            hex_msg = "".join(ch for ch in hex_msg.lower() if ch in "0123456789abcdef")
            if hex_msg and len(hex_msg) % 2 == 0:
                frames.append((stamp, hex_msg))
        return frames
