"""Raw ADS-B / Mode S frame types and dump1090 wire parsers."""

from __future__ import annotations

import math
from dataclasses import dataclass

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

BEAST_ESC = 0x1A
BEAST_MSG_LEN = {0x31: 2, 0x32: 7, 0x33: 14}
BEAST_TYPE_FOR_LEN = {2: 0x31, 7: 0x32, 14: 0x33}
BEAST_CLOCK_HZ = 12_000_000
BEAST_CLOCK_MOD = 1 << 48
_BEAST_BUF_MAX = 65_536
_CORRECT_WINDOW = 0.1
_CORRECT_HAMMING = 2
_MISSING_RSSI = -1_000.0
# Valid frame sizes in hex digits: Mode A/C (2 bytes), Mode S short (7), long (14).
_AVR_HEX_LENGTHS = frozenset({4, 14, 28})
# dump1090/mlat-client stamp synthetic MLAT results with this magic clock.
BEAST_MLAT_CLOCK = 0xFF004D4C4154
# A chunk never spans more than this; larger clock deltas mean the 12 MHz
# clock is not monotonic (merged streams, MLAT marker, receiver restart).
_MAX_CLOCK_SPREAD_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class RawFrame:
    """One Mode S / ADS-B frame as received from a sensor.

    ``timestamp`` is unix seconds (engine receive time). ``rssi`` is dBFS.
    ``clock`` is the dump1090 48-bit timestamp in 12 MHz ticks
    (1 tick = 1/12_000_000 s), when the transport provides it.
    ``bearing`` / ``elevation`` are the measured angle of arrival in degrees
    (true north / above horizon) and ``cfo`` the carrier frequency offset in
    Hz, from receivers with a coherent antenna array.
    """

    hex: str
    timestamp: float
    receiver: str = ""
    rssi: float | None = None
    clock: int | None = None
    bearing: float | None = None
    elevation: float | None = None
    cfo: float | None = None


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
    """JSON object for one raw frame on the live Redis ``live:raw`` channel."""
    item = frame_fields(frame.hex)
    item["timestamp"] = frame.timestamp
    if frame.receiver:
        item["receiver"] = frame.receiver
    if frame.rssi is not None and math.isfinite(frame.rssi):
        item["rssi"] = round(frame.rssi, 1)
    if frame.clock is not None:
        item["clock"] = frame.clock
    return item


def _rssi_rank(frame: RawFrame) -> float:
    if frame.rssi is not None and math.isfinite(frame.rssi):
        return frame.rssi
    return _MISSING_RSSI


def _better_copy(candidate: RawFrame, current: RawFrame) -> bool:
    """Prefer stronger RSSI; break ties with a hardware clock."""
    cand_rssi = _rssi_rank(candidate)
    cur_rssi = _rssi_rank(current)
    if cand_rssi != cur_rssi:
        return cand_rssi > cur_rssi
    if candidate.clock is not None and current.clock is None:
        return True
    return False


def _hamming_bits(left: bytes, right: bytes) -> int:
    if len(left) != len(right):
        return 1 << 30
    return sum((a ^ b).bit_count() for a, b in zip(left, right, strict=True))


def _frame_identity(hex_msg: str) -> tuple[object, str | None]:
    """DF and ICAO used to decide whether two frames are the same aircraft."""
    fields = frame_fields(hex_msg)
    df = fields.get("df")
    icao = fields.get("icao")
    if not isinstance(icao, str) and len(hex_msg) >= 8:
        icao = hex_msg[2:8]
    return df, icao if isinstance(icao, str) else None


def correct_receiver_frames(
    frames: list[RawFrame],
    *,
    window: float = _CORRECT_WINDOW,
    max_hamming: int = _CORRECT_HAMMING,
) -> list[RawFrame]:
    """Merge copies of the same transmission from multiple receivers.

    dump1090 CRC-corrects each radio independently, but two receivers can still
    disagree on a bit or two. Exact hex duplicates keep the stronger RSSI.
    Near-identical payloads from *different* receivers within *window* seconds
    (same DF/ICAO, Hamming distance ``<= max_hamming``) collapse to the
    stronger copy.
    """
    if not frames:
        return []
    by_hex: dict[str, RawFrame] = {}
    order: list[str] = []
    for frame in frames:
        prev = by_hex.get(frame.hex)
        if prev is None:
            by_hex[frame.hex] = frame
            order.append(frame.hex)
        elif _better_copy(frame, prev):
            by_hex[frame.hex] = frame
    unique = [by_hex[hex_msg] for hex_msg in order]
    kept: list[RawFrame] = []
    payloads: list[bytes] = []
    # Candidate index: (payload length, DF, ICAO) -> time bin -> kept indices.
    # Two frames within ``window`` seconds are at most one bin apart, so only
    # three bins need checking instead of every kept frame (was O(n^2)).
    buckets: dict[tuple[int, object, str | None], dict[int, list[int]]] = {}
    for frame in unique:
        try:
            payload = bytes.fromhex(frame.hex)
        except ValueError:
            kept.append(frame)
            payloads.append(b"")
            continue
        if not payload:
            kept.append(frame)
            payloads.append(payload)
            continue
        bins = buckets.setdefault((len(payload), *_frame_identity(frame.hex)), {})
        time_bin = _time_bin(frame.timestamp, window)
        candidates = sorted({
            index
            for offset in (-1, 0, 1)
            for index in bins.get(time_bin + offset, ())
        })
        merged = False
        for index in candidates:
            other = kept[index]
            if abs(frame.timestamp - other.timestamp) > window:
                continue
            if frame.receiver and other.receiver and frame.receiver == other.receiver:
                continue
            if _hamming_bits(payload, payloads[index]) > max_hamming:
                continue
            if _better_copy(frame, other):
                kept[index] = frame
                payloads[index] = payload
                # Keep the index findable from the replacement's time too.
                new_bin = _time_bin(frame.timestamp, window)
                if index not in bins.setdefault(new_bin, []):
                    bins[new_bin].append(index)
            merged = True
            break
        if not merged:
            bins.setdefault(time_bin, []).append(len(kept))
            kept.append(frame)
            payloads.append(payload)
    return kept


def _time_bin(timestamp: float, window: float) -> int:
    if window <= 0 or not math.isfinite(timestamp):
        return 0
    return math.floor(timestamp / window)


def beast_level_from_dbfs(rssi: float | None) -> int:
    """Inverse of :func:`beast_rssi_dbfs` (dump1090-fa ``sqrt(signal)*255``)."""
    if rssi is None or not math.isfinite(rssi):
        return 0
    level = round(255.0 * (10.0 ** (rssi / 20.0)))
    return max(0, min(255, int(level)))


def _escape_beast(data: bytes) -> bytes:
    out = bytearray()
    for byte in data:
        out.append(byte)
        if byte == BEAST_ESC:
            out.append(BEAST_ESC)
    return bytes(out)


def encode_beast(
    hex_msg: str,
    *,
    clock: int | None = None,
    rssi: float | None = None,
    timestamp: float | None = None,
) -> bytes:
    """Encode one Mode S / ADS-B frame as dump1090 Beast binary.

    Returns empty bytes when the payload length is not a Beast Mode A/C (2),
    Mode S short (7), or Mode S long (14) message.
    """
    try:
        payload = bytes.fromhex(hex_msg)
    except ValueError:
        return b""
    kind = BEAST_TYPE_FOR_LEN.get(len(payload))
    if kind is None:
        return b""
    if clock is None:
        wall = timestamp if timestamp is not None else 0.0
        clock = int(wall * BEAST_CLOCK_HZ) % BEAST_CLOCK_MOD
    clock = int(clock) % BEAST_CLOCK_MOD
    body = clock.to_bytes(6, "big") + bytes([beast_level_from_dbfs(rssi)]) + payload
    return bytes([BEAST_ESC, kind]) + _escape_beast(body)


def encode_beast_messages(messages: list[dict[str, object]]) -> bytes:
    """Encode a Redis ``live:raw`` batch as concatenated Beast frames.

    Hardware 12 MHz clocks are receiver-local. Dual-receiver merge would mix
    two oscillators on one dump1090-compatible feed, so the outbound clock is
    always derived from engine receive time (one wall-clock domain).
    """
    out = bytearray()
    for item in messages:
        hex_msg = item.get("hex")
        if not isinstance(hex_msg, str) or not hex_msg:
            continue
        rssi = item.get("rssi")
        timestamp = item.get("timestamp")
        out.extend(
            encode_beast(
                hex_msg,
                clock=None,
                rssi=rssi if isinstance(rssi, (int, float)) else None,
                timestamp=timestamp if isinstance(timestamp, (int, float)) else None,
            )
        )
    return bytes(out)


def parse_avr_line(line: str, *, strict: bool = True) -> tuple[str, int | None] | None:
    """Parse one dump1090 AVR line (``*HEX;`` or ``@CLOCKHEX;``).

    Returns ``(hex, clock)`` where *clock* is the 48-bit timestamp in
    12 MHz ticks from the ``@`` form, or ``None`` for classic ``*`` AVR.

    With *strict* (live feeds) the payload must be pure hex of a real frame
    size (4, 14 or 28 digits); otherwise stray characters are dropped and any
    even-length payload is accepted (hand-written replay fixtures).
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
    hex_msg = hex_msg.strip()
    if strict:
        if len(hex_msg) not in _AVR_HEX_LENGTHS or not set(hex_msg) <= _HEX_DIGITS:
            return None
    else:
        hex_msg = "".join(ch for ch in hex_msg if ch in _HEX_DIGITS)
        if not hex_msg or len(hex_msg) % 2:
            return None
    return hex_msg.lower(), clock


def receive_times(wall: float, clocks: list[int | None]) -> list[float]:
    """Spread a shared recv wall time across frames using 12 MHz tick deltas.

    *wall* is when this process saw the TCP chunk (after the last frame).
    Each frame with a clock is ``wall - (last_clock - clock) / 12e6``.
    Frames without a clock, with the MLAT marker clock, or whose clock is not
    plausibly before the reference (non-monotonic: merged streams, receiver
    restart) keep *wall* rather than landing months in the past via the 48-bit
    wrap. Equal results are jittered by 1 µs so history's
    ``(flight_id, timestamp)`` key stays unique.
    """
    usable = [
        None if clock is None or clock == BEAST_MLAT_CLOCK else clock
        for clock in clocks
    ]
    known = [clock for clock in usable if clock is not None]
    if not known:
        times = [wall] * len(clocks)
    else:
        last = known[-1]
        times = []
        for clock in usable:
            if clock is None:
                times.append(wall)
                continue
            delta = ((last - clock) % BEAST_CLOCK_MOD) / BEAST_CLOCK_HZ
            if delta > _MAX_CLOCK_SPREAD_SECONDS:
                times.append(wall)
                continue
            times.append(wall - delta)
    used: set[float] = set()
    unique: list[float] = []
    for raw_stamp in times:
        stamp = raw_stamp
        while stamp in used:
            stamp += 1e-6
        used.add(stamp)
        unique.append(stamp)
    return unique


def beast_rssi_dbfs(level: int) -> float | None:
    """Convert a dump1090 Beast signal byte to dBFS.

    dump1090-fa encodes ``sqrt(signalLevel) * 255``. JSON RSSI is
    ``10 * log10(signalLevel)``, which is ``20 * log10(byte / 255)``.
    """
    if level <= 0:
        return None
    return 20.0 * math.log10(level / 255.0)


def _read_unescaped(
    data: bytes | bytearray, start: int, count: int
) -> tuple[bytes | None, int | None]:
    """Read *count* payload bytes from *start*, undoing Beast ``0x1a 0x1a`` escapes.

    Returns ``(payload, next_index)`` on success. ``(None, None)`` means the
    buffer is incomplete. ``(None, resync_index)`` means a bare ``0x1a`` starts
    a new frame at *resync_index*; the caller should consume up to that ESC.
    """
    end = start + count
    # Fast path: most frames carry no ESC byte, so a plain slice is the payload.
    if end <= len(data) and data.find(BEAST_ESC, start, end) < 0:
        return bytes(data[start:end]), end
    out = bytearray()
    index = start
    while len(out) < count:
        if index >= len(data):
            return None, None
        byte = data[index]
        index += 1
        if byte == BEAST_ESC:
            if index >= len(data):
                return None, None
            if data[index] != BEAST_ESC:
                return None, index - 1
            index += 1
            out.append(BEAST_ESC)
        else:
            out.append(byte)
    return bytes(out), index


def try_parse_beast(
    buffer: bytes | bytearray, offset: int = 0
) -> tuple[tuple[str, float | None, int] | None, int]:
    """Pull one Beast frame from *buffer* starting at *offset*.

    Returns ``(frame, consumed)``, *consumed* counted from *offset*. It is 0
    when more data is needed. *frame* is ``(hex, rssi, clock)`` or ``None``
    when skipping garbage.
    """
    found = buffer.find(BEAST_ESC, offset)
    if found < 0:
        return None, len(buffer) - offset
    start = found

    if start + 2 > len(buffer):
        return None, start - offset

    kind = buffer[start + 1]
    if kind == BEAST_ESC:
        return None, start + 1 - offset
    msg_len = BEAST_MSG_LEN.get(kind)
    if msg_len is None:
        return None, start + 1 - offset

    body, next_index = _read_unescaped(buffer, start + 2, 6 + 1 + msg_len)
    if body is None:
        if next_index is not None:
            resync = next_index
            if resync <= start:
                return None, start + 1 - offset
            return None, resync - offset
        return None, start - offset
    clock = int.from_bytes(body[:6], "big")
    rssi = beast_rssi_dbfs(body[6])
    hex_msg = body[7:].hex()
    return (hex_msg, rssi, clock), next_index - offset


class BeastParser:
    """Incremental parser for dump1090 Beast binary (port 30005)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[tuple[str, float | None, int]]:
        self._buf.extend(chunk)
        frames: list[tuple[str, float | None, int]] = []
        offset = 0
        while offset < len(self._buf):
            frame, consumed = try_parse_beast(self._buf, offset)
            if consumed == 0:
                break
            if frame is not None:
                frames.append(frame)
            offset += consumed
        # One trim per chunk instead of a memmove per frame.
        if offset:
            del self._buf[:offset]
        if len(self._buf) > _BEAST_BUF_MAX:
            self._buf.clear()
        return frames
