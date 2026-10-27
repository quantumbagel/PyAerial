from __future__ import annotations

from helpers import make_config
from pyaerial.config.schema import ReceiverConfig, ReceiverPositionConfig
from pyaerial.receivers.aoa import parse_aoa_line
from pyaerial.receivers.positions import (
    SOURCE_CONFIG,
    SOURCE_HOME,
    SOURCE_RECEIVER_JSON,
    ReceiverPositions,
    default_position_url,
    parse_receiver_json,
)


def test_parse_receiver_json():
    assert parse_receiver_json({"lat": 35.1, "lon": -78.2}) == (35.1, -78.2, None)
    assert parse_receiver_json({"lat": 35.1, "lon": -78.2, "alt_m_amsl": 90}) == (35.1, -78.2, 90.0)
    assert parse_receiver_json({"lat": 0, "lon": 0}) is None  # unset in dump1090
    assert parse_receiver_json({"lat": "x", "lon": 1}) is None
    assert parse_receiver_json([]) is None


def test_default_position_url():
    assert (
        default_position_url(ReceiverConfig(type="dump1090", host="pi.local"))
        == "http://pi.local:8080/data/receiver.json"
    )
    assert default_position_url(ReceiverConfig(type="replay")) is None
    custom = ReceiverConfig(type="aoa", position_url="http://k/receiver.json")
    assert default_position_url(custom) == "http://k/receiver.json"


def test_config_position_wins_and_home_is_fallback(monkeypatch):
    config = make_config(
        receivers={
            "fixed": ReceiverConfig(
                type="dump1090",
                position=ReceiverPositionConfig(latitude=36.0, longitude=-79.0, altitude=120),
            ),
            "auto": ReceiverConfig(type="dump1090", host="feeder"),
        }
    )
    positions = ReceiverPositions(config)
    assert positions.get("fixed").source == SOURCE_CONFIG
    assert positions.get("fixed").altitude == 120
    assert positions.get("auto").source == SOURCE_HOME
    assert positions.get("auto").latitude == config.home.latitude

    fetched: list[str] = []

    def fake_fetch(url: str):
        fetched.append(url)
        return 35.9, -78.9, None

    monkeypatch.setattr(ReceiverPositions, "_fetch", staticmethod(fake_fetch))
    positions.refresh()
    assert fetched == ["http://feeder:8080/data/receiver.json"]
    auto = positions.get("auto")
    assert (auto.latitude, auto.longitude, auto.source) == (35.9, -78.9, SOURCE_RECEIVER_JSON)

    positions.set("auto", 35.0, -78.0, 300.0)
    assert positions.get("auto").altitude == 300.0
    assert positions.get("unknown").latitude == config.home.latitude


def test_parse_aoa_line():
    line = '{"hex": "8D4840D6202CC371C32CE0576098", "bearing": 370, "elevation": 4.5, "cfo": -1200, "rssi": -9.5}'
    parsed = parse_aoa_line(line)
    assert parsed["hex"] == "8d4840d6202cc371c32ce0576098"
    assert parsed["bearing"] == 10.0
    assert parsed["elevation"] == 4.5
    assert parsed["cfo"] == -1200.0
    assert parsed["clock"] is None
    assert parse_aoa_line('{"hex": "zz"}') is None
    assert parse_aoa_line("not json") is None
    assert parse_aoa_line('{"hex": "8d4840d6202cc371c32ce0576098", "elevation": 120}')["elevation"] is None
