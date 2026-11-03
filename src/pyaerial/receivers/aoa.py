"""Receiver for angle-of-arrival sidecars that stream JSON lines over TCP.

dump1090 has no phase information, so bearings come from a separate DSP chain
on a coherent multi-channel SDR (KrakenSDR, bladeRF 2.0, USRP B210/N310). It
connects here (or we connect to it) and writes one JSON object per line::

    {"hex": "8d4840d6202cc371c32ce0576098", "bearing": 213.4,
     "elevation": 4.1, "cfo": -18250.0, "rssi": -12.5, "clock": 123456789}

``hex`` is required. ``bearing`` is degrees clockwise from true north at the
antenna, ``elevation`` degrees above the horizon, ``cfo`` the carrier offset
in Hz, ``rssi`` dBFS, ``clock`` 12 MHz ticks. A ``time`` field is accepted but
ignored: frames are stamped with local receive time like every other
receiver's, and precise timing comes from ``clock``.

Options: ``host`` / ``port`` of the sidecar. The integrity checks also read
``bearing_offset`` (degrees added to every bearing, for array orientation) and
``array_axis`` (degrees; set it for a linear array whose bearings are
mirror-ambiguous about that axis).
"""

from __future__ import annotations

import contextlib
import json
import math
import socket
import time

from pyaerial.receivers import Receiver, register_receiver
from pyaerial.receivers.frames import _AVR_HEX_LENGTHS, _HEX_DIGITS, BEAST_CLOCK_MOD

_RECV_BUFFER = 65_536
_SOCKET_TIMEOUT = 1.0
_LINE_MAX = 4096
# We never write to the sidecar, so a half-open connection (sidecar host
# rebooted) is only noticed by silence.
_IDLE_TIMEOUT = 150.0


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def parse_aoa_line(line: str) -> dict[str, object] | None:
    """Validate one sidecar JSON line. Returns emit kwargs plus ``hex``/``time``."""
    try:
        doc = json.loads(line)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    hex_msg = doc.get("hex")
    if not isinstance(hex_msg, str):
        return None
    hex_msg = hex_msg.strip().lower()
    if len(hex_msg) not in _AVR_HEX_LENGTHS or not set(hex_msg) <= _HEX_DIGITS:
        return None
    clock = doc.get("clock")
    if isinstance(clock, bool) or not isinstance(clock, int):
        clock = None
    elif not 0 <= clock < BEAST_CLOCK_MOD:
        clock = None
    bearing = _number(doc.get("bearing"))
    elevation = _number(doc.get("elevation"))
    return {
        "hex": hex_msg,
        "time": _number(doc.get("time")),
        "rssi": _number(doc.get("rssi")),
        "clock": clock,
        "bearing": bearing % 360.0 if bearing is not None else None,
        "elevation": elevation
        if elevation is not None and -90 <= elevation <= 90
        else None,
        "cfo": _number(doc.get("cfo")),
    }


@register_receiver("aoa")
class AoAReceiver(Receiver):
    def configure(self, arguments: dict) -> None:
        self.ip = str(
            arguments.get("tcp_connection_ip") or arguments.get("host") or "localhost"
        )
        port = arguments.get("tcp_connection_port") or arguments.get("port")
        if port is None:
            raise ValueError("aoa receiver requires a port")
        self.port = int(port)

    def run(self) -> str | None:
        try:
            client = socket.create_connection(
                (self.ip, self.port), timeout=_SOCKET_TIMEOUT
            )
        except OSError as exc:
            return f"failed to connect to {self.ip}:{self.port} ({exc})"
        client.settimeout(_SOCKET_TIMEOUT)
        self.log.info("Connected to AoA sidecar at %s:%s", self.ip, self.port)
        buffer = ""
        last_data = time.monotonic()
        try:
            while not self.should_stop():
                try:
                    chunk = client.recv(_RECV_BUFFER)
                except TimeoutError:
                    if time.monotonic() - last_data > _IDLE_TIMEOUT:
                        return f"no data for {_IDLE_TIMEOUT:.0f}s"
                    continue
                except ConnectionResetError:
                    return "connection reset by peer"
                except OSError as exc:
                    return f"socket error ({exc})"
                if not chunk:
                    return "socket connection closed by peer"
                last_data = time.monotonic()
                buffer += chunk.decode("utf-8", errors="ignore")
                lines = buffer.split("\n")
                buffer = lines.pop()
                if len(buffer) > _LINE_MAX:
                    buffer = ""
                now = time.time()
                for line in lines:
                    parsed = parse_aoa_line(line)
                    if parsed is None:
                        continue
                    # Local receive time, never the sidecar's own: frame
                    # times from every receiver must share one clock or the
                    # merge can keep a copy stamped ahead of the others and
                    # the tracker then drops their (older) samples. Precise
                    # timing comes from the 12 MHz ``clock`` instead.
                    parsed.pop("time")
                    hex_msg = parsed.pop("hex")
                    self.emit(hex_msg, now, **parsed)
        finally:
            with contextlib.suppress(OSError):
                client.close()
        return None
