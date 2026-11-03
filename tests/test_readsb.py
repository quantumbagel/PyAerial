"""readsb receiver: registration, config, the BeastReduce guard, receiver.json
locations, and parsing of real readsb Beast streams."""

from __future__ import annotations

import logging
import socket
import threading
from pathlib import Path

import pytest

from helpers import IDENT, make_config, with_crc
from pyaerial.config.loader import ConfigError, load_config
from pyaerial.config.schema import ReceiverConfig
from pyaerial.receivers import (
    ReceiverEndpoint,
    create_receiver,
    receiver_class,
    receiver_sources,
)
from pyaerial.receivers.dump1090 import Dump1090Receiver
from pyaerial.receivers.frames import (
    BEAST_ESC,
    BEAST_MLAT_CLOCK,
    BeastParser,
    beast_rssi_dbfs,
)
from pyaerial.receivers.positions import (
    SOURCE_HOME,
    SOURCE_RECEIVER_JSON,
    FetchError,
    ReceiverPositions,
    default_position_url,
    default_position_urls,
    parse_receiver_json,
)
from pyaerial.receivers.readsb import ReadsbReceiver

_HOME = "home: {latitude: 35.7275, longitude: -78.6959}\n"


def _load(tmp_path: Path, receiver_yaml: str):
    path = tmp_path / "config.yaml"
    path.write_text(_HOME + "receivers:\n  pi:\n" + receiver_yaml)
    return load_config(path)


# --- registration and config ------------------------------------------------------


def test_readsb_is_a_builtin_dump1090_variant():
    assert receiver_class("readsb") is ReadsbReceiver
    assert receiver_sources()["readsb"] == "builtin"
    assert issubclass(ReadsbReceiver, Dump1090Receiver)
    assert ReadsbReceiver.known_options == Dump1090Receiver.known_options | {
        "allow_reduced"
    }
    # The dump1090 contract is untouched.
    assert "allow_reduced" not in Dump1090Receiver.known_options


def test_top_level_host_port_and_format_reach_readsb(tmp_path):
    config = _load(
        tmp_path, "    type: readsb\n    host: feeder.lan\n    port: 30005\n"
    )
    args = config.receivers["pi"].receiver_arguments()
    assert ReadsbReceiver.endpoint(args) == ReceiverEndpoint(
        "feeder.lan", 30005, "beast"
    )

    config = _load(
        tmp_path, "    type: readsb\n    host: feeder.lan\n    port: 30002\n"
    )
    args = config.receivers["pi"].receiver_arguments()
    assert ReadsbReceiver.endpoint(args).wire_format == "avr"

    with pytest.raises(ConfigError, match="format"):
        _load(tmp_path, "    type: readsb\n    port: 30002\n    format: beast\n")


def test_reduce_port_is_rejected_with_an_escape_hatch(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        _load(tmp_path, "    type: readsb\n    host: feeder.lan\n    port: 30006\n")
    message = str(excinfo.value)
    assert "receivers.pi" in message and "BeastReduce" in message
    assert "allow_reduced: true" in message and "30005" in message

    config = _load(
        tmp_path,
        "    type: readsb\n    port: 30006\n    options: {allow_reduced: true}\n",
    )
    assert config.receivers["pi"].options == {"allow_reduced": True}

    with pytest.raises(
        ConfigError, match="options.allow_reduced: must be true or false"
    ):
        _load(
            tmp_path, "    type: readsb\n    options: {allow_reduced: 'yes please'}\n"
        )


def test_dump1090_type_is_not_subject_to_the_reduce_check(tmp_path):
    config = _load(tmp_path, "    type: dump1090\n    port: 30006\n")
    assert config.receivers["pi"].type == "dump1090"
    with pytest.raises(ConfigError, match="unknown option 'allow_reduced'"):
        _load(tmp_path, "    type: dump1090\n    options: {allow_reduced: true}\n")


def test_readsb_reading_the_portal_beast_port_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="web.beast_port"):
        _load(tmp_path, "    type: readsb\n    port: 30105\n")


# --- receiver.json -----------------------------------------------------------------


def test_readsb_position_urls_try_tar1090_layouts_in_order():
    readsb = ReceiverConfig(type="readsb", host="pi.local")
    assert default_position_urls(readsb) == [
        "http://pi.local/tar1090/data/receiver.json",
        "http://pi.local:8504/data/receiver.json",
        "http://pi.local:8080/data/receiver.json",
    ]
    assert default_position_urls(ReceiverConfig(type="readsb"))[0] == (
        "http://localhost/tar1090/data/receiver.json"
    )
    assert default_position_urls(ReceiverConfig(type="readsb", host="fd00::2"))[1] == (
        "http://[fd00::2]:8504/data/receiver.json"
    )
    custom = ReceiverConfig(type="readsb", position_url="http://pi/rx.json")
    assert default_position_urls(custom) == ["http://pi/rx.json"]
    # dump1090 keeps its single dump1090-fa default.
    dump1090 = ReceiverConfig(type="dump1090", host="pi.local")
    assert default_position_urls(dump1090) == [
        "http://pi.local:8080/data/receiver.json"
    ]
    assert default_position_url(dump1090) == "http://pi.local:8080/data/receiver.json"


def test_parse_readsb_receiver_json():
    # json_out.c generateReceiverJson(): lat/lon with 6 decimals; alt_m_amsl
    # only when gpsd supplied one; no lat/lon with --json-location-accuracy 0.
    doc = {
        "refresh": 1000,
        "history": 120,
        "lat": 35.727488,
        "lon": -78.695942,
        "jaeroTimeout": 33.0,
        "readsb": True,
        "version": "wiedehopf git: 1a2b3c4",
    }
    assert parse_receiver_json(doc) == (35.727488, -78.695942, None)
    assert parse_receiver_json({**doc, "alt_m_amsl": 97}) == (
        35.727488,
        -78.695942,
        97.0,
    )
    hidden = {k: v for k, v in doc.items() if k not in ("lat", "lon")}
    assert parse_receiver_json(hidden) is None


def _readsb_positions(monkeypatch, answers: dict[str, object]):
    config = make_config(receivers={"pi": ReceiverConfig(type="readsb", host="pi")})
    positions = ReceiverPositions(config)
    fetched: list[str] = []

    def fake_fetch(url: str):
        fetched.append(url)
        answer = answers.get(url, FetchError(f"HTTP 404 from {url}"))
        if isinstance(answer, FetchError):
            raise answer
        return answer

    monkeypatch.setattr(ReceiverPositions, "_fetch", staticmethod(fake_fetch))
    return positions, fetched


def test_position_candidates_are_tried_in_order_and_the_hit_pinned(monkeypatch):
    port_8504 = "http://pi:8504/data/receiver.json"
    positions, fetched = _readsb_positions(
        monkeypatch,
        {
            # Answers, but has no location (--json-location-accuracy 0).
            "http://pi/tar1090/data/receiver.json": None,
            port_8504: (35.912345, -78.912345, None),
        },
    )
    positions.refresh(now=0.0)
    assert fetched == ["http://pi/tar1090/data/receiver.json", port_8504]
    got = positions.get("pi")
    assert (got.latitude, got.longitude, got.source) == (
        35.912345,
        -78.912345,
        SOURCE_RECEIVER_JSON,
    )

    fetched.clear()
    positions.refresh(now=1_000.0)
    assert fetched == [port_8504]


def test_all_position_candidates_failing_backs_off(monkeypatch):
    positions, fetched = _readsb_positions(monkeypatch, {})
    positions.refresh(now=0.0)
    assert len(fetched) == 3
    assert positions.get("pi").source == SOURCE_HOME
    fetched.clear()
    positions.refresh(now=1.0)  # still backing off
    assert fetched == []


def test_rounded_receiver_json_position_warns(monkeypatch, caplog):
    positions, _ = _readsb_positions(
        monkeypatch, {"http://pi/tar1090/data/receiver.json": (35.73, -78.7, None)}
    )
    with caplog.at_level(logging.WARNING, logger="pyaerial.receivers.positions"):
        positions.refresh(now=0.0)
    assert any("looks rounded" in r.getMessage() for r in caplog.records)
    assert positions.get("pi").latitude == 35.73  # still better than home


# --- readsb Beast streams -------------------------------------------------------------


def _escape(data: bytes) -> bytes:
    return data.replace(bytes([BEAST_ESC]), bytes([BEAST_ESC, BEAST_ESC]))


def _readsb_beast(frames: list[tuple[str, int, int, int | None]]) -> bytes:
    """Beast bytes as net_io.c modesSendBeastOutput() writes them: with
    --net-receiver-id, a ``0x1a 0xe3`` + 8-byte (escaped) receiver id block
    whenever the id changes, then ``0x1a`` type + 6-byte clock + signal +
    payload, all 0x1a-escaped."""
    out = bytearray()
    last_id: int | None = None
    for hex_msg, clock, signal, receiver_id in frames:
        if receiver_id is not None and receiver_id != last_id:
            last_id = receiver_id
            out += bytes([BEAST_ESC, 0xE3]) + _escape(receiver_id.to_bytes(8, "big"))
        payload = bytes.fromhex(hex_msg)
        kind = {2: 0x31, 7: 0x32, 14: 0x33}[len(payload)]
        body = clock.to_bytes(6, "big") + bytes([signal]) + payload
        out += bytes([BEAST_ESC, kind]) + _escape(body)
    return bytes(out)


DF11 = "5d484fdea248f5"
DF17_ESC = with_crc("8d1a1a1a2015a678d4d220")  # 0x1a in the address
# Receiver ids whose escaped bytes look like frame starts if mis-aligned:
# "1a 33", "1a 32", a trailing 0x1a right before the next frame's ESC.
RX_IDS = [
    0x1A33_1A32_0000_1A1A,
    0x0102_0304_0506_071A,
    0x1A1A_1A1A_1A1A_1A1A,
    0xDEAD_BEEF_0000_0001,
]
# 0x1a 0x31 + 6 zero clock bytes + zero signal + 0000: readsb's (and
# dump1090-fa's) idle heartbeat, a Mode A/C frame.
HEARTBEAT = ("0000", 0, 0, None)


def _stream() -> tuple[bytes, list[tuple[str, float | None, int]]]:
    frames = [
        (IDENT, 0x0000_0012_3456, 0x80, RX_IDS[0]),
        (DF11, 0x0000_0012_3500, 0x1A, RX_IDS[0]),
        (DF17_ESC, 0x0000_1A1A_1A1A, 0x40, RX_IDS[1]),
        # --forward-mlat: a synthetic MLAT result with the magic clock.
        (IDENT, BEAST_MLAT_CLOCK, 0xFF, RX_IDS[1]),
        (IDENT, 0x0000_0012_9999, 0x7F, RX_IDS[2]),
        (DF11, 0x0000_0013_0000, 0x10, RX_IDS[3]),
        (IDENT, 0x0000_0013_0001, 0x11, RX_IDS[0]),
        HEARTBEAT,
    ]
    expected = [(h, beast_rssi_dbfs(s), c) for h, c, s, _rid in frames]
    return _readsb_beast(frames), expected


def test_readsb_stream_with_receiver_ids_loses_and_invents_nothing():
    stream, expected = _stream()
    assert stream.count(bytes([BEAST_ESC, 0xE3])) >= 5  # the id blocks are there
    assert BeastParser().feed(stream) == expected


def test_readsb_stream_parses_identically_in_any_chunking():
    stream, expected = _stream()
    for size in (1, 2, 3, 7, 11, 64):
        parser = BeastParser()
        got: list = []
        for start in range(0, len(stream), size):
            got.extend(parser.feed(stream[start : start + size]))
        assert got == expected, size


def test_unknown_escaped_blocks_are_skipped():
    """Types readsb never writes on a Beast output (Mode-S Beast 0x34 status,
    say) are skipped as long as they are 0x1a-escaped like every Beast type."""
    status = bytes([BEAST_ESC, 0x34]) + _escape(
        bytes([0, 0, 0x1A, 0x33, 0, 1, 0xFF, 0x1A])
    )
    ping = bytes([BEAST_ESC, ord("P")]) + _escape(bytes([0x1A, 0x31, 0x1A]))
    frame = _readsb_beast([(IDENT, 5, 0x80, None)])
    assert (
        BeastParser().feed(status + frame + ping + frame)
        == [(IDENT, beast_rssi_dbfs(0x80), 5)] * 2
    )


def test_readsb_receiver_emits_every_frame_of_a_live_stream(caplog):
    stream, expected = _stream()
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]

    def serve():
        conn, _ = server.accept()
        for start in range(0, len(stream), 5):
            conn.sendall(stream[start : start + 5])
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    emitted: list[tuple[str, float | None, int | None]] = []
    receiver = create_receiver(
        "readsb",
        "pi",
        lambda hex_msg, _ts, **kw: emitted.append((hex_msg, kw["rssi"], kw["clock"])),
        {"tcp_connection_ip": "127.0.0.1", "tcp_connection_port": port},
    )
    with caplog.at_level(logging.INFO, logger="pyaerial.receiver.pi"):
        reason = receiver.run()
    server.close()
    assert reason == "socket connection closed by peer"
    assert emitted == expected
    assert any("Connected to readsb beast" in r.getMessage() for r in caplog.records)
