from __future__ import annotations

from pyaerial.receivers.dump1090 import Dump1090Receiver
from pyaerial.receivers.frames import (
    BEAST_ESC,
    BeastParser,
    RawFrame,
    _read_unescaped,
    beast_rssi_dbfs,
    correct_receiver_frames,
    encode_beast,
    encode_beast_messages,
    merge_receiver_frames,
    parse_avr_line,
    raw_payload,
    receive_times,
    try_parse_beast,
)


def _emit(*_args, **_kwargs) -> None:
    return None


def test_parse_avr_star_line():
    parsed = parse_avr_line("*8D406B902015A678D4D220AA4BDA;\n")
    assert parsed == ("8d406b902015a678d4d220aa4bda", None)


def test_parse_avr_timestamped_line():
    parsed = parse_avr_line("@0000000000018D406B902015A678D4D220AA4BDA;")
    assert parsed is not None
    hex_msg, clock = parsed
    assert hex_msg == "8d406b902015a678d4d220aa4bda"
    assert clock == 1


def test_parse_avr_ignores_junk():
    assert parse_avr_line("") is None
    assert parse_avr_line("hello") is None
    assert parse_avr_line("*zz;") is None


def test_raw_payload_includes_df_icao_rssi():
    frame = RawFrame(
        hex="8d406b902015a678d4d220aa4bda",
        timestamp=1.5,
        receiver="main",
        rssi=-12.34,
        clock=99,
    )
    payload = raw_payload(frame)
    assert payload["hex"] == "8d406b902015a678d4d220aa4bda"
    assert payload["df"] == 17
    assert payload["icao"] == "406b90"
    assert payload["rssi"] == -12.3
    assert payload["clock"] == 99
    assert payload["receiver"] == "main"
    assert "receivers" not in payload

    with_heard = raw_payload(frame, receivers=("main", "north"))
    assert with_heard["receiver"] == "main"
    assert with_heard["receivers"] == ["main", "north"]


def test_raw_payload_omits_missing_rssi():
    payload = raw_payload(RawFrame(hex="8d406b90", timestamp=1.0, receiver="r"))
    assert "rssi" not in payload
    assert "clock" not in payload
    assert "receivers" not in raw_payload(
        RawFrame(hex="8d406b90", timestamp=1.0, receiver="r"), receivers=()
    )


def _beast_frame(hex_msg: str, *, clock: int = 1, signal: int = 128) -> bytes:
    body = clock.to_bytes(6, "big") + bytes([signal]) + bytes.fromhex(hex_msg)
    kind = {2: 0x31, 7: 0x32, 14: 0x33}[len(hex_msg) // 2]
    escaped = bytearray([BEAST_ESC, kind])
    for byte in body:
        escaped.append(byte)
        if byte == BEAST_ESC:
            escaped.append(BEAST_ESC)
    return bytes(escaped)


def test_beast_parser_mode_s_long():
    hex_msg = "8d406b902015a678d4d220aa4bda"
    parser = BeastParser()
    frames = parser.feed(_beast_frame(hex_msg, clock=12, signal=128))
    assert len(frames) == 1
    got_hex, rssi, clock = frames[0]
    assert got_hex == hex_msg
    assert clock == 12
    assert rssi is not None
    assert abs(rssi - beast_rssi_dbfs(128)) < 1e-9


def test_beast_parser_escapes_0x1a_in_payload():
    hex_msg = "8d1a6b902015a678d4d220aa4bda"
    parser = BeastParser()
    frames = parser.feed(_beast_frame(hex_msg, clock=1, signal=0x1A))
    assert len(frames) == 1
    got_hex, rssi, clock = frames[0]
    assert got_hex == hex_msg
    assert clock == 1
    assert rssi == beast_rssi_dbfs(0x1A)


def test_beast_parser_incomplete_then_complete():
    hex_msg = "8d406b902015a678d4d220aa4bda"
    raw = _beast_frame(hex_msg)
    parser = BeastParser()
    assert parser.feed(raw[:5]) == []
    frames = parser.feed(raw[5:])
    assert len(frames) == 1
    assert frames[0][0] == hex_msg


def test_try_parse_beast_skips_garbage():
    hex_msg = "8d406b902015a678d4d220aa4bda"
    buffer = b"\x00\xff" + _beast_frame(hex_msg)
    frame, consumed = try_parse_beast(buffer)
    assert frame is not None
    assert frame[0] == hex_msg
    assert consumed == len(buffer)


def test_receive_times_spreads_chunk_using_clock_deltas():
    stamps = receive_times(100.0, [12_000_000, 18_000_000, 24_000_000])
    assert stamps[-1] == 100.0
    assert abs(stamps[0] - 99.0) < 1e-9
    assert abs(stamps[1] - 99.5) < 1e-9
    assert stamps[0] < stamps[1] < stamps[2]


def test_receive_times_uniquifies_identical_clocks():
    stamps = receive_times(10.0, [1, 1, None])
    assert stamps[0] != stamps[1]
    assert len(set(stamps[:2])) == 2


def test_beast_parser_resyncs_on_bare_esc_in_payload():
    hex_msg = "8d406b902015a678d4d220aa4bda"
    good = _beast_frame(hex_msg, clock=2)
    payload, hint = _read_unescaped(bytes([0x00, 0x01, BEAST_ESC, 0x33]), 0, 10)
    assert payload is None
    assert hint == 2
    # Truncated long frame, then a bare ESC that starts the real frame.
    garbage = bytes([BEAST_ESC, 0x33, 0x11, 0x22]) + good
    parser = BeastParser()
    frames = parser.feed(garbage)
    assert any(frame[0] == hex_msg for frame in frames)


def test_dump1090_defaults_to_beast():
    receiver = Dump1090Receiver("main", _emit, {})
    assert receiver.format == "beast"
    assert receiver.port == 30005


def test_dump1090_avr_port_implies_avr():
    receiver = Dump1090Receiver("main", _emit, {"tcp_connection_port": 30002})
    assert receiver.format == "avr"
    assert receiver.port == 30002


def test_dump1090_beast_format_defaults_port():
    receiver = Dump1090Receiver("main", _emit, {"format": "beast"})
    assert receiver.format == "beast"
    assert receiver.port == 30005


def test_dump1090_port_30005_implies_beast():
    receiver = Dump1090Receiver(
        "main", _emit, {"tcp_connection_port": 30005}
    )
    assert receiver.format == "beast"
    assert receiver.port == 30005


def test_dump1090_explicit_avr_on_beast_port():
    receiver = Dump1090Receiver(
        "main", _emit, {"tcp_connection_port": 30005, "format": "avr"}
    )
    assert receiver.format == "avr"
    assert receiver.port == 30005


def test_encode_beast_roundtrips_long_frame():
    hex_msg = "8d406b902015a678d4d220aa4bda"
    raw = encode_beast(hex_msg, clock=12, rssi=beast_rssi_dbfs(128))
    frames = BeastParser().feed(raw)
    assert len(frames) == 1
    got_hex, rssi, clock = frames[0]
    assert got_hex == hex_msg
    assert clock == 12
    assert rssi is not None
    assert abs(rssi - beast_rssi_dbfs(128)) < 0.15


def test_encode_beast_escapes_0x1a():
    hex_msg = "8d1a6b902015a678d4d220aa4bda"
    raw = encode_beast(hex_msg, clock=1, rssi=beast_rssi_dbfs(0x1A))
    frames = BeastParser().feed(raw)
    assert frames[0][0] == hex_msg
    assert frames[0][2] == 1


def test_encode_beast_messages_skips_unknown_length():
    data = encode_beast_messages(
        [
            {"hex": "8d406b90", "timestamp": 1.0},
            {
                "hex": "8d406b902015a678d4d220aa4bda",
                "timestamp": 1.0,
                "clock": 9,
                "rssi": -12.3,
            },
        ]
    )
    frames = BeastParser().feed(data)
    assert [frame[0] for frame in frames] == ["8d406b902015a678d4d220aa4bda"]
    assert frames[0][2] == 12_000_000


def test_encode_beast_messages_uses_receive_time_clock():
    hex_msg = "8d406b902015a678d4d220aa4bda"
    data = encode_beast_messages(
        [{"hex": hex_msg, "timestamp": 2.0, "clock": 99, "rssi": -12.0}]
    )
    frames = BeastParser().feed(data)
    assert frames[0][0] == hex_msg
    assert frames[0][2] == 24_000_000


def test_correct_receiver_frames_keeps_stronger_rssi():
    hex_msg = "8d406b902015a678d4d220aa4bda"
    weak = RawFrame(hex=hex_msg, timestamp=1.0, receiver="a", rssi=-30.0, clock=1)
    strong = RawFrame(hex=hex_msg, timestamp=1.01, receiver="b", rssi=-12.0, clock=2)
    merged = correct_receiver_frames([weak, strong])
    assert len(merged) == 1
    assert merged[0].receiver == "b"
    assert merged[0].clock == 2


def test_correct_receiver_frames_hamming_vote():
    good = "8d406b902015a678d4d220aa4bda"
    flipped = "8d406b902015a678d4d220aa4bdb"
    weak = RawFrame(hex=flipped, timestamp=1.0, receiver="a", rssi=-28.0, clock=1)
    strong = RawFrame(hex=good, timestamp=1.02, receiver="b", rssi=-10.0, clock=2)
    merged = correct_receiver_frames([weak, strong])
    assert len(merged) == 1
    assert merged[0].hex == good
    assert merged[0].receiver == "b"


def test_merge_receiver_frames_lists_every_receiver_strongest_first():
    good = "8d406b902015a678d4d220aa4bda"
    flipped = "8d406b902015a678d4d220aa4bdb"
    frames = [
        RawFrame(hex=flipped, timestamp=1.0, receiver="a", rssi=-28.0),
        RawFrame(hex=good, timestamp=1.01, receiver="c", rssi=-20.0),
        RawFrame(hex=good, timestamp=1.02, receiver="b", rssi=-10.0),
    ]
    merged = merge_receiver_frames(frames)
    assert len(merged) == 1
    frame, heard = merged[0]
    assert frame.receiver == "b"
    assert heard[0] == "b"
    assert sorted(heard) == ["a", "b", "c"]


def test_correct_receiver_frames_leaves_same_receiver_alone():
    a = RawFrame(
        hex="8d406b902015a678d4d220aa4bda",
        timestamp=1.0,
        receiver="main",
        rssi=-10.0,
    )
    b = RawFrame(
        hex="8d406b902015a678d4d220aa4bdb",
        timestamp=1.01,
        receiver="main",
        rssi=-11.0,
    )
    merged = correct_receiver_frames([a, b])
    assert len(merged) == 2


def test_correct_receiver_frames_skips_different_icao():
    a = RawFrame(
        hex="8d406b902015a678d4d220aa4bda",
        timestamp=1.0,
        receiver="a",
        rssi=-10.0,
    )
    b = RawFrame(
        hex="8d406b912015a678d4d220aa4bda",
        timestamp=1.01,
        receiver="b",
        rssi=-11.0,
    )
    merged = correct_receiver_frames([a, b])
    assert {frame.hex for frame in merged} == {a.hex, b.hex}


def _reference_merge(frames: list[RawFrame], window: float = 0.1) -> list[RawFrame]:
    """The original O(n^2) merge, kept as an oracle for the indexed version."""
    from pyaerial.receivers.frames import _better_copy, _frame_identity, _hamming_bits

    by_hex: dict[str, RawFrame] = {}
    order: list[str] = []
    for frame in frames:
        prev = by_hex.get(frame.hex)
        if prev is None:
            by_hex[frame.hex] = frame
            order.append(frame.hex)
        elif _better_copy(frame, prev):
            by_hex[frame.hex] = frame
    kept: list[RawFrame] = []
    for frame in (by_hex[h] for h in order):
        payload = bytes.fromhex(frame.hex)
        for index, other in enumerate(kept):
            other_payload = bytes.fromhex(other.hex)
            if len(other_payload) != len(payload):
                continue
            if abs(frame.timestamp - other.timestamp) > window:
                continue
            if frame.receiver == other.receiver:
                continue
            if _frame_identity(frame.hex) != _frame_identity(other.hex):
                continue
            if _hamming_bits(payload, other_payload) > 2:
                continue
            if _better_copy(frame, other):
                kept[index] = frame
            break
        else:
            kept.append(frame)
    return kept


def test_correct_receiver_frames_matches_reference_merge():
    import random

    rng = random.Random(42)
    bases = ["8d%06x%022x" % (rng.randrange(8), rng.getrandbits(88)) for _ in range(12)]
    frames = []
    for i in range(600):
        payload = bytearray(bytes.fromhex(rng.choice(bases)))
        for _ in range(rng.choice((0, 0, 1, 2, 3))):
            payload[rng.randrange(4, 14)] ^= 1 << rng.randrange(8)
        frames.append(
            RawFrame(
                hex=payload.hex(),
                timestamp=1.7e9 + i * 0.013 + rng.uniform(-0.05, 0.05),
                receiver=rng.choice("ab"),
                rssi=rng.choice((None, -10.0, -20.0, -30.0)),
            )
        )
    assert correct_receiver_frames(frames) == _reference_merge(frames)


def test_correct_receiver_frames_is_not_quadratic():
    """Regression: 10k frames took ~3.8 s with the all-pairs scan."""
    import random
    import time

    rng = random.Random(1)
    frames = [
        RawFrame(
            hex="8d%06x%022x" % (rng.randrange(1 << 24), rng.getrandbits(88)),
            timestamp=1.7e9 + i * 1e-5,
            receiver="ab"[i % 2],
        )
        for i in range(10_000)
    ]
    started = time.perf_counter()
    merged = correct_receiver_frames(frames)
    assert time.perf_counter() - started < 0.5
    assert len(merged) == len(frames)


def test_receive_times_ignores_non_monotonic_clock():
    """Regression: a clock after the reference wrapped to ~271 days ago."""
    stamps = receive_times(100.0, [12_000_000, 24_000_000, 18_000_000])
    assert stamps[1] == 100.0  # later than the reference -> wall time
    assert abs(stamps[0] - 99.5) < 1e-9
    assert min(stamps) > 97.0


def test_receive_times_ignores_mlat_marker_clock():
    from pyaerial.receivers.frames import BEAST_MLAT_CLOCK

    stamps = receive_times(100.0, [12_000_000, 18_000_000, BEAST_MLAT_CLOCK])
    assert abs(stamps[0] - 99.5) < 1e-9
    assert abs(stamps[1] - 100.0) < 1e-5
    assert abs(stamps[2] - 100.0) < 1e-5


def test_parse_avr_rejects_bad_lengths_and_garbage():
    assert parse_avr_line("*8D406B902015A678D4D220AA4B;") is None  # 26 digits
    assert parse_avr_line("*8D406B902015A6;") == ("8d406b902015a6", None)
    assert parse_avr_line("*7700;") == ("7700", None)
    assert parse_avr_line("*8D406B90zz15A678D4D220AA4BDA;") is None
    assert parse_avr_line("AABBCC", strict=False) == ("aabbcc", None)


def test_beast_parser_same_frames_for_any_chunking():
    frames_in = [
        ("8d406b902015a678d4d220aa4bda", 5, 128),
        ("8d1a6b902015a678d4d220aa4bda", 0x1A1A, 0x1A),
        ("5d484fdea248f5", 77, 200),
    ]
    stream = b"\x00garbage" + b"".join(
        _beast_frame(h, clock=c, signal=s) for h, c, s in frames_in
    )
    whole = BeastParser().feed(stream)
    parser = BeastParser()
    split: list = []
    for i in range(len(stream)):
        split.extend(parser.feed(stream[i : i + 1]))
    assert [f[0] for f in whole] == [h for h, _c, _s in frames_in]
    assert split == whole
