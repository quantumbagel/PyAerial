"""Raw ADS-B / Mode S frame types and dump1090 wire parsers."""

from __future__ import annotations

import math
from dataclasses import dataclass

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

BEAST_ESC = 0x1A
BEAST_MSG_LEN = {0x31: 2, 0x32: 7, 0x33: 14}
_BEAST_BUF_MAX = 65_536


@dataclass(frozen=True, slots=True)
class RawFrame:
    """One Mode S / ADS-B frame as received from a sensor.

    ``timestamp`` is unix seconds (engine receive time). ``rssi`` is dBFS.
    ``clock`` is the dump1090 48-bit timestamp in 12 MHz ticks
    (1 tick = 1/12_000_000 s), when the transport provides it.
    """

    hex: str
    timestamp: float
    receiver: str = ""
    rssi: float | None = None
    clock: int | None = None


def frame_fields(hex_msg: str) -> dict[str, object]:
    """Cheap header fields from a Mode S hex frame (no full classification)."""
    hex_msg = hex_msg.lower().strip()
    fields: dict[str, object] = {"hex": hex_msg}
    if len(hex_msg) < 2 or len(hex_msg) % 2:
        return fields
    try:
        df = (int(hex_msg[:2], 16) >> 3) & 0x1F
    except ValueError:
        return fields
    fields["df"] = df
    if df in (17, 18) and len(hex_msg) >= 8:
        fields["icao"] = hex_msg[2:8]
    return fields


def raw_payload(frame: RawFrame) -> dict[str, object]:
    """JSON object for one raw frame on the ``/ws/raw`` websocket."""
    item = frame_fields(frame.hex)
    item["timestamp"] = frame.timestamp
    if frame.receiver:
        item["receiver"] = frame.receiver
    if frame.rssi is not None and math.isfinite(frame.rssi):
        item["rssi"] = round(frame.rssi, 1)
    if frame.clock is not None:
        item["clock"] = frame.clock
    return item


def parse_avr_line(line: str) -> tuple[str, int | None] | None:
    """Parse one dump1090 AVR line (``*HEX;`` or ``@CLOCKHEX;``).

    Returns ``(hex, clock)`` where *clock* is the 48-bit timestamp in
    12 MHz ticks from the ``@`` form, or ``None`` for classic ``*`` AVR.
    """
    text = line.strip()
    if not text:
        return None
    clock: int | None = None
    if text.startswith("@") and text.endswith(";"):
        body = text[1:-1]
        if len(body) > 12:
            clock_hex, body = body[:12], body[12:]
            try:
                clock = int(clock_hex, 16)
            except ValueError:
                body = clock_hex + body
                clock = None
        hex_msg = body
    else:
        hex_msg = text.replace("*", "").replace(";", "")
    hex_msg = "".join(ch for ch in hex_msg if ch in _HEX_DIGITS)
    if not hex_msg or len(hex_msg) % 2:
        return None
    return hex_msg.lower(), clock


def beast_rssi_dbfs(level: int) -> float | None:
    """Convert a dump1090 Beast signal byte to dBFS.

    dump1090-fa encodes ``sqrt(signalLevel) * 255``. JSON RSSI is
    ``10 * log10(signalLevel)``, which is ``20 * log10(byte / 255)``.
    """
    if level <= 0:
        return None
    return 20.0 * math.log10(level / 255.0)


def _read_unescaped(data: bytes, start: int, count: int) -> tuple[bytes, int] | None:
    """Read *count* payload bytes from *start*, undoing Beast ``0x1a 0x1a`` escapes.

    Returns ``(payload, next_index)``. ``None`` means the buffer is incomplete
    (need more data). A bare ``0x1a`` that starts a new frame also returns
    ``None`` so the caller can resync.
    """
    out = bytearray()
    index = start
    while len(out) < count:
        if index >= len(data):
            return None
        byte = data[index]
        index += 1
        if byte == BEAST_ESC:
            if index >= len(data):
                return None
            if data[index] != BEAST_ESC:
                return None
            index += 1
            out.append(BEAST_ESC)
        else:
            out.append(byte)
    return bytes(out), index


def try_parse_beast(buffer: bytes) -> tuple[tuple[str, float | None, int] | None, int]:
    """Pull one Beast frame from *buffer*.

    Returns ``(frame, consumed)``. *consumed* is 0 when more data is needed.
    *frame* is ``(hex, rssi, clock)`` or ``None`` when skipping garbage.
    """
    try:
        start = buffer.index(BEAST_ESC)
    except ValueError:
        return None, len(buffer)

    if start + 2 > len(buffer):
        return None, start

    kind = buffer[start + 1]
    if kind == BEAST_ESC:
        return None, start + 1
    msg_len = BEAST_MSG_LEN.get(kind)
    if msg_len is None:
        return None, start + 1

    payload = _read_unescaped(buffer, start + 2, 6 + 1 + msg_len)
    if payload is None:
        if start:
            return None, start
        return None, 0

    body, next_index = payload
    clock = int.from_bytes(body[:6], "big")
    rssi = beast_rssi_dbfs(body[6])
    hex_msg = body[7:].hex()
    return (hex_msg, rssi, clock), next_index


class BeastParser:
    """Incremental parser for dump1090 Beast binary (port 30005)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[tuple[str, float | None, int]]:
        self._buf.extend(chunk)
        frames: list[tuple[str, float | None, int]] = []
        while self._buf:
            frame, consumed = try_parse_beast(self._buf)
            if consumed == 0:
                break
            if frame is not None:
                frames.append(frame)
            del self._buf[:consumed]
        if len(self._buf) > _BEAST_BUF_MAX:
            self._buf.clear()
        return frames
