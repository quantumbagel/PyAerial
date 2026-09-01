"""Replay a recorded dump1090-style raw hex file."""

from __future__ import annotations

import time
from pathlib import Path

from pyaerial.receivers import Receiver, register_receiver


@register_receiver("replay")
class ReplayReceiver(Receiver):
    """Play hex frames from a text file (optional ``timestamp hex`` per line)."""

    def configure(self, arguments: dict) -> None:
        path = arguments.get("path")
        if not path:
            raise ValueError("replay receiver requires options.path")
        self.path = Path(str(path))
        self.speed = float(arguments.get("speed", 1.0)) or 1.0
        self.loop = bool(arguments.get("loop", True))
        self.interval = float(arguments.get("interval", 0.1))

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
                return "replay finished"
        return None

    def _load(self) -> list[tuple[float, str]]:
        frames: list[tuple[float, str]] = []
        sequential = 0.0
        for raw in self.path.read_text(errors="ignore").splitlines():
            line = raw.strip().replace("*", "").replace(";", "")
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                try:
                    stamp = float(parts[0])
                    hex_msg = parts[1]
                except ValueError:
                    stamp = sequential
                    hex_msg = parts[0]
                    sequential += self.interval
            else:
                stamp = sequential
                hex_msg = parts[0]
                sequential += self.interval
            if hex_msg:
                frames.append((stamp, hex_msg))
        return frames
