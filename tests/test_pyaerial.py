import math

import pytest

from pyaerial import calculations
from pyaerial.requirements_eval import RequirementError, collect_component_names, eval_requirement
from pyaerial.rosetta import filter_packets
from pyaerial.validator import validate_config


def test_eval_requirement_simple():
    assert eval_requirement("a and b", {"a": True, "b": True})
    assert not eval_requirement("a and b", {"a": True, "b": False})
    assert eval_requirement("a or b", {"a": False, "b": True})


def test_eval_requirement_not():
    assert eval_requirement("not a", {"a": False})
    assert not eval_requirement("not a", {"a": True})


def test_collect_component_names():
    assert set(collect_component_names("lenient and critical")) == {"lenient", "critical"}


def test_eval_requirement_rejects_unknown_name():
    with pytest.raises(RequirementError):
        eval_requirement("a and evil", {"a": True})


def test_eval_requirement_rejects_call():
    with pytest.raises(RequirementError):
        eval_requirement("foo()", {"foo": True})


def test_filter_packets_all_none_decimate():
    packets = [(1, 0.0), (2, 1.0), (3, 2.0), (4, 3.0)]
    assert len(filter_packets(packets, "decimate(2)")) == 2


def test_time_to_enter_geofence_inside():
    square = [[0.0, 0.0], [0.0, 1.0], [1.0, 1.0], [1.0, 0.0]]
    t = calculations.time_to_enter_geofence([0.5, 0.5], 90.0, 100.0, square, 3600)
    assert t == 0


def test_time_to_enter_geofence_no_speed():
    square = [[0.0, 0.0], [0.0, 1.0], [1.0, 1.0], [1.0, 0.0]]
    t = calculations.time_to_enter_geofence([2.0, 0.5], 90.0, 0.0, square, 3600)
    assert t == math.inf


def _minimal_valid_config():
    return {
        "general": {
            "mongodb": "mongodb://localhost:27017",
            "backdate_packets": 10,
            "remember_planes": 30,
            "status_message_top_planes": 5,
            "advanced_status": True,
            "hz": 2,
            "duplicate_packet_merging": 5,
            "logs": "info",
        },
        "home": {"latitude": 36.68, "longitude": -78.87},
        "receivers": {
            "main": {
                "method": "dump1090",
                "arguments": {"tcp_connection_ip": "localhost", "tcp_connection_port": "30002"},
            }
        },
        "components": {
            "easy": {"altitude": {"maximum": 10000}},
        },
        "zones": {
            "z1": {
                "coordinates": [[35.0, -79.0], [35.1, -79.0], [35.1, -78.9], [35.0, -78.9]],
                "levels": {
                    "warn": {"category": "c1", "requirements": "easy", "seconds": 1},
                },
            }
        },
        "categories": {
            "c1": {
                "alert_method": "print",
                "save": {
                    "telemetry": {"default": "all"},
                    "calculated": {"default": "all"},
                },
            }
        },
    }


def test_validate_config_minimal():
    cfg = _minimal_valid_config()
    issues = validate_config(cfg)
    assert not any(s == "error" for s, _ in issues)


def test_validate_config_missing_receiver():
    cfg = _minimal_valid_config()
    del cfg["receivers"]
    issues = validate_config(cfg)
    assert any("receivers" in m.lower() for _, m in issues)
