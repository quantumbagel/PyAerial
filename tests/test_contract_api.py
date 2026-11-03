"""Golden fixtures for the portal's public API contract.

Pins, as checked-in JSON under ``tests/golden/``:

* ``api_hello.json``: the ``hello`` message from ``websocket_hello()``;
* ``api_spec.json``: ``GET /api`` from the real FastAPI app;
* ``api_push_<type>.json``: one pushed ``/ws/live`` message of each stream
  (``flights``, ``alerts``, ``stats``, ``telemetry``), produced by the real
  ``LiveBroadcaster._broadcast_tick`` over a live store (fake Redis) that a
  real ``AlertEngine`` activation and ``write_live_planes`` filled.

Times are frozen and every time-valued field (``*_at``, ``*_time``,
``timestamp``) is stored relative to the frozen clock (``T0 + offset`` is
written as ``"T0+offset"``), so the fixtures are stable and still show which
times are equal.

Regenerate after an intended change, then review the diff (breaking changes
need a new ``protocol``/``version``)::

    PYAERIAL_UPDATE_GOLDEN=1 .venv/bin/python -m pytest tests/test_contract_api.py
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from helpers import LOCAL_REDIS_URI, episode_recorder, make_config
from pyaerial.alerts import engine as engine_mod
from pyaerial.alerts.engine import AlertEngine
from pyaerial.api.broadcaster import LiveBroadcaster, _Client
from pyaerial.api.spec import WS_STREAMS, websocket_hello
from pyaerial.calc.geo import build_polygons
from pyaerial.calc.motion import ResolvedMotion
from pyaerial.constants import (
    STORE_ALT,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_LAT,
    STORE_LONG,
)
from pyaerial.models import AircraftInfo, Datum, Plane, TrackState
from pyaerial.store.live.writer import LiveWriter
from pyaerial.webapp import create_app
from test_contract_webhook import check_golden

T0 = 1_750_000_000.0
_UUID = uuid.UUID("00000000-0000-4000-8000-000000000002")
_TIME_KEYS = ("timestamp",)
_TIME_SUFFIXES = ("_at", "_time", "_updated")


def _relative_times(data: object, key: str = "") -> object:
    if isinstance(data, dict):
        return {k: _relative_times(v, k) for k, v in data.items()}
    if isinstance(data, list):
        return [_relative_times(v, key) for v in data]
    is_time = key in _TIME_KEYS or key.endswith(_TIME_SUFFIXES)
    if is_time and isinstance(data, (int, float)) and not isinstance(data, bool):
        offset = round(float(data) - T0, 3)
        return f"T0{offset:+g}"
    return data


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: T0)
    monkeypatch.setattr(engine_mod.uuid, "uuid4", lambda: _UUID)


def test_hello_matches_golden():
    check_golden("api_hello.json", websocket_hello())


def test_api_spec_matches_golden():
    app = create_app(
        config=make_config(), history=None, live_store=None, aircraft_db=None
    )
    with TestClient(app) as client:
        response = client.get("/api")
    assert response.status_code == 200
    check_golden("api_spec.json", response.json())


def _plane() -> Plane:
    t = T0 - 5
    return Plane(
        info=AircraftInfo(
            icao="A1B2C3",
            callsign="N123AB",
            registration="N123AB",
            model="Cessna 172S",
            aircraft_type="C172",
            integrity={
                "score": 0.95,
                "status": "ok",
                "checks": {"kinematics": {"status": "pass", "detail": "consistent"}},
            },
        ),
        track=TrackState(first_packet=T0 - 600, last_update=t, packets=42),
        received_data={
            STORE_LAT: [Datum(35.722, t - 2), Datum(35.725, t)],
            STORE_LONG: [Datum(-78.695, t - 2), Datum(-78.695, t)],
            STORE_ALT: [Datum(457.2, t - 2), Datum(457.2, t)],
        },
        calculated_data={
            STORE_HORIZ_SPEED: [Datum(185.2, t)],
            STORE_HEADING: [Datum(0.0, t)],
        },
    )


def _pushed_messages(monkeypatch) -> dict[str, dict]:
    config = make_config()
    config.zones["pad"].rules[0].dwell_seconds = 1
    store = LiveWriter(LOCAL_REDIS_URI)
    engine = AlertEngine(config, build_polygons(config.zones), episode_recorder(store))
    plane = _plane()
    motion = ResolvedMotion(heading_deg=0.0, speed_kph=185.2, turn_rate_deg_s=0.0)
    mono = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: mono[0])
    try:
        for _ in range(2):  # hold, then activate inside the pad zone
            engine.check(plane, (35.725, -78.695), motion, "N123AB")
            mono[0] += 1.0
    finally:
        engine.close()
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    store.touch_engine()
    store.write_live_planes({"A1B2C3": plane})

    async def _tick() -> list[dict]:
        broadcaster = LiveBroadcaster(store, None, history=None)
        client = _Client(telemetry_floor=0.0, last_ping=T0)
        broadcaster._clients = {object(): client}  # type: ignore[dict-item]
        await broadcaster._broadcast_tick()
        return client.outbox.messages()

    try:
        messages = asyncio.run(_tick())
    finally:
        store.close()
    return {message["type"]: message for message in messages}


def test_pushed_messages_match_golden(fake, frozen, monkeypatch):
    messages = _pushed_messages(monkeypatch)
    assert set(messages) == set(WS_STREAMS)
    # Non-empty samples: the fixture shows real field sets, not [].
    assert messages["flights"]["flights"] and messages["alerts"]["alerts"]
    assert messages["telemetry"]["telemetry"]
    assert messages["flights"]["flights"][0]["active_alerts"]
    for kind in sorted(WS_STREAMS):
        check_golden(f"api_push_{kind}.json", _relative_times(messages[kind]))
