"""Golden fixtures for the public webhook contract (``pyaerial.alert/1``).

A fixed aircraft goes through the real :class:`AlertEngine` (meta/payload
building) and :class:`WebhookAlerter` (body building) for one activate, one
while_active and one deactivate; each JSON body must equal the checked-in
``tests/golden/webhook_<event>.json``. Any change to the contract shows up as
a diff of those files.

Regenerate after an intended change (and review the diff; after 1.0 only new
keys are allowed within ``pyaerial.alert/1``)::

    PYAERIAL_UPDATE_GOLDEN=1 .venv/bin/python -m pytest tests/test_contract_webhook.py
"""

from __future__ import annotations

import json
import os
import time
import typing
import uuid
from pathlib import Path

import pytest

from helpers import make_config, make_rule
from pyaerial.alerters import contract
from pyaerial.alerters.webhook import WebhookAlerter
from pyaerial.alerts import engine as engine_mod
from pyaerial.alerts.engine import AlertEngine
from pyaerial.calc.geo import build_polygons
from pyaerial.calc.motion import ResolvedMotion
from pyaerial.config.schema import AlertActionConfig, WhileActiveConfig, ZoneConfig
from pyaerial.constants import (
    STORE_ALT,
    STORE_CALC_DATA,
    STORE_FIRST_PACKET,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_MOST_RECENT_PACKET,
    STORE_RECV_DATA,
    STORE_VERT_SPEED,
)
from pyaerial.models import Datum

GOLDEN = Path(__file__).parent / "golden"
_UPDATE = os.environ.get("PYAERIAL_UPDATE_GOLDEN") == "1"
_T0 = 1_750_000_000.0
_UUID = uuid.UUID("00000000-0000-4000-8000-000000000001")
_URL = "https://hooks.example.com/pyaerial"


def _stable(data: object) -> object:
    """Floats rounded to 6 decimals: geodesic ETAs may differ in the last
    digits between library versions, which is not a contract change."""
    if isinstance(data, float):
        return round(data, 6)
    if isinstance(data, dict):
        return {key: _stable(value) for key, value in data.items()}
    if isinstance(data, (list, tuple)):
        return [_stable(value) for value in data]
    return data


def check_golden(name: str, data: object) -> None:
    """Compare *data* with ``tests/golden/<name>`` (or write it when updating)."""
    path = GOLDEN / name
    text = (
        json.dumps(_stable(data), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    if _UPDATE:
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        return
    assert path.exists(), f"{path} missing; run with PYAERIAL_UPDATE_GOLDEN=1"
    assert json.loads(path.read_text()) == json.loads(text), (
        f"{name} changed; if intended, regenerate with PYAERIAL_UPDATE_GOLDEN=1 "
        "and review the diff"
    )


class _Inline:
    def submit(self, key, fn, *args) -> bool:
        fn(*args)
        return True

    def close(self, timeout: float = 0.0) -> None:
        pass


def _config(fmt: str = "json"):
    action = [AlertActionConfig(method="webhook", options={"url": _URL, "format": fmt})]
    rule = make_rule(
        "low_approach", dwell_seconds=1, altitude={"max": 1000}, eta={"max": 120}
    )
    rule.color = "#ef4444"
    rule.on_activate = action
    rule.on_deactivate = action
    rule.while_active = WhileActiveConfig(interval_seconds=30, actions=action)
    other = make_rule("near", dwell_seconds=60, distance={"max": 5})
    return make_config(
        zones={
            "airport": ZoneConfig(
                coordinates=[
                    [35.72, -78.70],
                    [35.73, -78.70],
                    [35.73, -78.69],
                    [35.72, -78.69],
                    [35.72, -78.70],
                ],
                rules=[rule],
            ),
            "tower": ZoneConfig(
                coordinates=[[35.80, -78.60], [35.81, -78.60], [35.81, -78.59]],
                rules=[other],
            ),
        }
    )


def _plane() -> dict:
    t = _T0
    return {
        STORE_INFO: {
            STORE_ICAO: "A1B2C3",  # pyModeS hands out upper case
            "callsign": "N123AB",
            "registration": "N123AB",
            "model": "Cessna 172S",
            "aircraft_type": "C172",
            "owner": "Example Flying Club",
            "country": "United States",
        },
        STORE_RECV_DATA: {
            STORE_LAT: [Datum(35.70, t - 10)],
            STORE_LONG: [Datum(-78.695, t - 10)],
            STORE_ALT: [Datum(457.2, t - 10)],
            STORE_VERT_SPEED: [Datum(-2.54, t - 10)],
        },
        STORE_CALC_DATA: {
            STORE_HORIZ_SPEED: [Datum(185.2, t - 10)],
            STORE_HEADING: [Datum(0.0, t - 10)],
        },
        STORE_INTERNAL: {STORE_FIRST_PACKET: t - 600, STORE_MOST_RECENT_PACKET: t},
    }


def _run(monkeypatch, fmt: str = "json") -> list[dict]:
    clock = [0.0]
    monkeypatch.setattr(time, "time", lambda: _T0 + clock[0])
    monkeypatch.setattr(time, "monotonic", lambda: 1_000.0 + clock[0])
    monkeypatch.setattr(engine_mod.uuid, "uuid4", lambda: _UUID)
    bodies: list[dict] = []
    monkeypatch.setattr(
        WebhookAlerter, "_deliver", lambda self, body: bodies.append(body)
    )
    config = _config(fmt)
    engine = AlertEngine(config, build_polygons(config.zones))
    engine._dispatcher.close(timeout=0.0)
    engine._dispatcher = _Inline()  # type: ignore[assignment]
    plane = _plane()
    northbound = ResolvedMotion(heading_deg=0.0, speed_kph=185.2, turn_rate_deg_s=0.0)
    try:
        # 2.2 km south of the zone at 185 km/h: ETA ~43 s.
        for seconds in (0.0, 1.0, 31.0):  # match, activate, while_active
            clock[0] = seconds
            engine.check(plane, (35.70, -78.695), northbound, "N123AB")
        clock[0] = 32.0
        southbound = ResolvedMotion(
            heading_deg=180.0, speed_kph=185.2, turn_rate_deg_s=0.0
        )
        engine.check(plane, (35.70, -78.695), southbound, "N123AB")  # no ETA: cleared
    finally:
        engine.close()
    return bodies


def test_json_bodies_match_golden(monkeypatch):
    bodies = _run(monkeypatch)
    assert [b["event"] for b in bodies] == list(contract.EVENTS)
    for body in bodies:
        check_golden(f"webhook_{body['event']}.json", body)


def test_body_shape_matches_documented_contract(monkeypatch):
    documented = set(typing.get_type_hints(contract.AlertBody))
    telemetry = set(typing.get_type_hints(contract.AlertTelemetry))
    for body in _run(monkeypatch):
        assert set(body) == documented
        assert set(body["telemetry"]) == telemetry
        assert body["schema"] == "pyaerial.alert/1"
        assert body["event"] == body["reason"]["hook"]
        assert body["rule"] == body["type"]
        assert body["icao"] == body["icao"].lower()
        json.dumps(body, allow_nan=False)  # strict JSON
        for field in documented | telemetry:
            assert f"\n{field} " in contract.__doc__, f"{field} undocumented"


def test_one_alert_id_and_timestamps_across_events(monkeypatch):
    activate, while_active, deactivate = _run(monkeypatch)
    assert activate["alert_id"] == while_active["alert_id"] == deactivate["alert_id"]
    assert activate["activated_at"] == activate["timestamp"] == _T0 + 1
    assert while_active["activated_at"] == _T0 + 1
    assert while_active["deactivated_at"] is None and while_active["ended_by"] is None
    assert deactivate["deactivated_at"] == deactivate["timestamp"] == _T0 + 32
    assert deactivate["ended_by"] == "cleared"


@pytest.mark.parametrize("fmt", ["discord", "slack"])
def test_chat_formats_name_the_event(monkeypatch, fmt):
    bodies = _run(monkeypatch, fmt)
    texts = [json.dumps(b) for b in bodies]
    assert len(texts) == 3
    assert "Activated" in texts[0]
    assert "Still active" in texts[1] and "still meets" in texts[1]
    assert "Ended (conditions cleared)" in texts[2] and "no longer meets" in texts[2]
    if fmt == "discord":
        events = [b["embeds"][0]["fields"][0] for b in bodies]
        assert [e["name"] for e in events] == ["Event"] * 3
        assert bodies[0]["embeds"][0]["title"].endswith("(Activate)")
    else:
        assert bodies[2]["text"].startswith("[Ended (conditions cleared)]")
