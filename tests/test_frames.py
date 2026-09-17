from __future__ import annotations

from pyaerial.receivers.dump1090 import Dump1090Receiver
from pyaerial.receivers.frames import (
    BEAST_ESC,
    BeastParser,
    RawFrame,
    beast_rssi_dbfs,
    parse_avr_line,
    raw_payload,
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


def test_raw_payload_omits_missing_rssi():
    payload = raw_payload(RawFrame(hex="8d406b90", timestamp=1.0, receiver="r"))
    assert "rssi" not in payload
    assert "clock" not in payload


def _beast_frame(hex_msg: str, *, clock: int = 1, signal: int = 128) -> bytes:
    body = clock.to_bytes(6, "big") + bytes([signal]) + bytes.fromhex(hex_msg)
    escaped = bytearray([BEAST_ESC, 0x33])
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


def test_dump1090_defaults_to_avr():
    receiver = Dump1090Receiver("main", _emit, {})
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
