"""The wire models in ``pyaerial.api.models`` match what the portal sends.

Every payload builder's output is validated against its model (strict, unknown
keys forbidden), and dumping the validated model must give the same JSON back,
so a key the server omits stays omitted and a ``null`` stays ``null``. The
checked-in ``web/src/api/schema.json`` and ``generated.ts`` must match the
models.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from pathlib import Path
from typing import Any, get_args

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from helpers import make_config, make_plane, with_unknown_then_ok
from pyaerial.alerts.engine import AlertEngine
from pyaerial.api import models
from pyaerial.api.broadcaster import LiveBroadcaster, _Client
from pyaerial.api.models import (
    RESPONSES,
    Alert,
    AppConfig,
    ErrorResponse,
    FlightDetail,
    FlightSummary,
    LiveMessage,
    TelemetryPoint,
    ZonesData,
    dump,
)
from pyaerial.api.payloads import (
    app_config_payload,
    enrich_flight_detail,
    flight_summary,
    format_alert,
    zones_payload,
)
from pyaerial.api.spec import (
    WS_ACTIONS,
    WS_ERROR_CODES,
    WS_PROTOCOL,
    WS_STREAMS,
    websocket_hello,
)
from pyaerial.calc.geo import build_polygons
from pyaerial.calc.motion import ResolvedMotion
from pyaerial.config.schema import (
    ReceiverConfig,
    ReceiverPositionConfig,
    WebConfig,
)
from pyaerial.constants import (
    STORE_ALT,
    STORE_GNSS_HEIGHT,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_LAT,
    STORE_LONG,
)
from pyaerial.jsonutil import dumps as json_dumps
from pyaerial.models import AircraftInfo, Datum, Plane, TrackState
from pyaerial.store.archive import ArchiveReader, ArchiveWriter
from pyaerial.store.redis_live import RedisLiveStore
from pyaerial.webapp import create_app

ROOT = Path(__file__).resolve().parent.parent


def _gen_types():
    spec = importlib.util.spec_from_file_location(
        "gen_types", ROOT / "scripts" / "gen_types.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(model: type[BaseModel], payload: Any) -> Any:
    """Validate *payload* as sent (JSON text) and return it decoded."""
    text = payload if isinstance(payload, str) else json_dumps(payload)
    parsed = model.model_validate_json(text, strict=True)
    decoded = json.loads(text)
    # Omitted keys stay omitted and nulls stay null through the model.
    assert dump(parsed) == decoded
    return decoded


# -- generated files -----------------------------------------------------------


def test_generated_files_are_current():
    gen = _gen_types()
    for path, expected in gen.render().items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        assert current == expected, (
            f"{path.relative_to(ROOT)} is stale: the wire models changed. "
            f"Regenerate with `{gen.COMMAND}` and review the diff."
        )


def test_literals_match_protocol_constants():
    assert get_args(models.StreamName) == WS_STREAMS
    assert get_args(models.ActionName) == WS_ACTIONS
    assert get_args(models.ErrorCode) == WS_ERROR_CODES
    assert set(RESPONSES) == set(WS_ACTIONS)
    hello = models.HelloMessage.model_fields["protocol"].annotation
    assert get_args(hello) == (WS_PROTOCOL,)


def test_models_are_strict():
    flight = {"flight_id": "f", "icao": "a", "active_alerts": []}
    with pytest.raises(ValidationError):
        check(FlightSummary, flight)  # missing always-sent keys
    point = {"flight_id": "f", "timestamp": 1.0, "altitude": None}
    point |= {"speed": None, "heading": None}
    check(TelemetryPoint, point)
    with pytest.raises(ValidationError, match="extra"):
        check(TelemetryPoint, {**point, "bogus": 1})
    with pytest.raises(ValidationError):
        check(TelemetryPoint, {**point, "gnss_height": None})  # omitted, never null
    with pytest.raises(ValidationError):
        check(TelemetryPoint, {**point, "timestamp": "1"})


def test_hello_bytes():
    hello = websocket_hello()
    check(LiveMessage, hello)
    assert json_dumps(hello) == (
        '{"type":"hello","protocol":"pyaerial.live","version":1,'
        '"streams":["flights","alerts","telemetry","stats"],'
        '"actions":["subscribe","fetchFlights","fetchFlight","fetchTelemetry",'
        '"fetchAlerts","fetchStats","fetchZones","fetchConfig"]}'
    )


# -- live store and archive with every optional field in play ---------------------

def _alerting_plane(t: float) -> Plane:
    """Inside the ``pad`` zone, low and slow (as in test_contract_api)."""
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
        track=TrackState(first_packet=t - 600, last_update=t, packets=42),
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


def _spoofed_plane(first: float) -> Plane:
    """Outside every zone, GNSS height, provenance and a full integrity doc."""
    plane = make_plane(
        "C0FFEE",
        first,
        first + 2,
        callsign="SPOOF1",
        recv={
            STORE_LAT: [Datum(36.5 + n * 1e-3, first + n) for n in range(3)],
            STORE_LONG: [Datum(-79.5, first + n) for n in range(3)],
            STORE_ALT: [Datum(9000.0, first)],
            STORE_GNSS_HEIGHT: [Datum(9100.0, first + 1)],
        },
        calc={STORE_HORIZ_SPEED: [Datum(800.0, first + 2)]},
    )
    plane.info.integrity = {
        "score": 0.3,
        "status": "spoofed",
        "checks": {
            "kinematics": {"status": "pass", "detail": "consistent"},
            "tdoa": {"status": "fail", "detail": "4.2 us off", "value": 4.2},
        },
        "emitter": {"latitude": 35.9, "longitude": -78.9, "offset_km": 25.3},
        "rebroadcast": True,
    }
    return with_unknown_then_ok(plane)


def _quiet_plane(first: float) -> Plane:
    """Heard, but no position yet."""
    return make_plane(
        "BEEF01", first, first + 5, recv={STORE_HEADING: [Datum(90.0, first)]}
    )


@pytest.fixture
def stores(tmp_path, monkeypatch):
    config = make_config()
    config.zones["pad"].rules[0].dwell_seconds = 1
    store = RedisLiveStore("redis://localhost:1/0", memory_only=True, writer=True)
    # Recent: the live store drops telemetry older than telemetry_keep_seconds.
    now = time.time()
    alerting = _alerting_plane(now - 5)
    planes = {
        "A1B2C3": alerting,
        "C0FFEE": _spoofed_plane(now - 30),
        "BEEF01": _quiet_plane(now - 30),
    }
    engine = AlertEngine(config, build_polygons(config.zones), store)
    motion = ResolvedMotion(heading_deg=0.0, speed_kph=185.2, turn_rate_deg_s=0.0)
    mono = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: mono[0])
    try:
        for _ in range(2):  # hold, then activate inside the pad zone
            engine.check(alerting, (35.725, -78.695), motion, "N123AB")
            mono[0] += 1.0
    finally:
        engine.close()
    monkeypatch.undo()
    store.touch_engine()
    store.write_live_planes(planes)

    path = tmp_path / "archive.db"
    writer = ArchiveWriter(path)
    for plane in planes.values():
        alerts = store.get_alerts(flight_id=plane.flight_id, active_only=False)
        assert writer.finalize_plane(plane, alerts=alerts)
    writer.close()
    reader = ArchiveReader(path)
    try:
        yield config, store, reader, planes
    finally:
        reader.close()
        store.close()


def test_pushed_messages_validate(stores):
    _config, store, reader, _planes = stores

    async def _tick() -> list[str]:
        broadcaster = LiveBroadcaster(store, None, history=reader)
        client = _Client(telemetry_floor=0.0, last_ping=time.time())
        broadcaster._clients = {object(): client}  # type: ignore[dict-item]
        await broadcaster._broadcast_tick()
        broadcaster._send_pings(time.time() + 60)
        return [item.text for item in client.outbox._items]

    texts = asyncio.run(_tick())
    messages = {m["type"]: m for m in (check(LiveMessage, t) for t in texts)}
    assert set(messages) == {*WS_STREAMS, "ping"}
    points = messages["telemetry"]["telemetry"]
    assert any("gnss_height" in p for p in points)
    assert any("receivers" in p and "integrity_status" in p for p in points)
    flights = {f["icao"]: f for f in messages["flights"]["flights"]}
    assert flights["c0ffee"]["integrity"]["emitter"]
    assert flights["beef01"]["latitude"] is None


class _Rpc:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.pushed: list[dict] = []
        self._next = 0

    def __call__(self, action: str, **params: Any) -> dict:
        self._next += 1
        req_id = f"r{self._next}"
        self.ws.send_text(
            json.dumps(
                {"type": "request", "id": req_id, "action": action, "params": params}
            )
        )
        while True:
            text = self.ws.receive_text()
            message = json.loads(text)
            if message.get("type") != "response":
                self.pushed.append(check(LiveMessage, text))
                continue
            assert message["id"] == req_id
            model = RESPONSES[action] if message["success"] else ErrorResponse
            return check(model, text)


def test_rpc_responses_validate(stores):
    config, store, reader, planes = stores
    app = create_app(config=config, history=reader, live_store=store, aircraft_db=None)
    ids = {icao: plane.flight_id for icao, plane in planes.items()}
    with TestClient(app) as client, client.websocket_connect("/ws/live") as ws:
        rpc = _Rpc(ws)
        hello = check(LiveMessage, ws.receive_text())
        assert hello["type"] == "hello"
        for view in ("live", "history"):
            flights = rpc("fetchFlights", view=view)["data"]
            assert {f["flight_id"] for f in flights} == set(ids.values())
            for flight_id in ids.values():
                rpc("fetchFlight", flightId=flight_id, view=view)
                page = rpc("fetchTelemetry", flightId=flight_id, view=view)
                assert page["truncated"] is False
            assert rpc("fetchTelemetry", flightId=ids["C0FFEE"], view=view, limit=1)[
                "truncated"
            ]
            assert rpc("fetchAlerts", view=view, flightId=ids["A1B2C3"])["data"]
        live_quiet = rpc("fetchFlight", flightId=ids["BEEF01"], view="live")["data"]
        assert "latitude" not in live_quiet and "retained" not in live_quiet
        history_spoofed = rpc("fetchFlight", flightId=ids["C0FFEE"], view="history")
        assert history_spoofed["data"]["alert_stats"] is None
        rpc("fetchStats")
        rpc("fetchZones")
        rpc("fetchConfig")
        assert rpc("subscribe", streams=["flights", "stats"])["data"]["streams"]
        assert rpc("subscribe", streams=["nope"])["code"] == "bad_request"
        assert rpc("fetchFlight", flightId="nope")["code"] == "not_found"
        assert rpc("fetchTelemetry")["code"] == "bad_request"
        assert rpc("fetchNothing")["code"] == "bad_request"
    kinds = {message["type"] for message in rpc.pushed}
    assert {"flights", "alerts", "stats"} <= kinds


# -- builder branches the stores above do not reach -----------------------------------


def test_alert_variants():
    stored_reason = {
        "alert_id": "f:z:r",
        "flight_id": "f",
        "icao": "abc123",
        "callsign": "UNKNOWN",
        "zone": "z",
        "rule": "r",
        "active": False,
        "activated_at": 1.0,
        "deactivated_at": 2.0,
        "eta": None,
        "reason": json.dumps({"ended_by": "cleared"}),
        "position": {"type": "Point", "coordinates": [-78.6, 35.7]},
        "altitude": 100,
    }
    alert = check(Alert, format_alert(stored_reason))
    assert alert["ended_by"] == "cleared" and alert["callsign"] is None
    bare = check(Alert, format_alert({"flight_id": "f", "zone": "z", "rule": "r"}))
    assert bare["alert_id"] == "f:z:r"


def test_history_summary_and_detail_variants():
    doc = {
        "_id": "abc123-1",
        "icao": "abc123",
        "status": "completed",
        "start_time": 1.0,
        "end_time": 9.0,
        "retained": True,
        "callsign": "",
        "info": {},
        "active_alerts": [],
    }
    without = check(FlightSummary, flight_summary(doc, None, None))
    assert "alert_stats" not in without and without["timestamp"] is None
    stats = {"episode_count": 1, "total_seconds": 5, "active_count": 0}
    last = {"timestamp": 8.0, "altitude": 10.0, "speed": None, "heading": None}
    summary = flight_summary({**doc, "alert_stats": stats}, last, None)
    assert check(FlightSummary, summary)["latitude"] is None
    detail = {k: v for k, v in summary.items() if k != "retained"}
    check(FlightDetail, enrich_flight_detail(detail, "abc123", None))


def test_zones_and_config_variants():
    config = make_config(
        receivers={
            "main": ReceiverConfig(
                type="dump1090",
                format="avr",
                position=ReceiverPositionConfig(latitude=35.0, longitude=-78.0),
            ),
            "file": ReceiverConfig(type="replay", options={"format": "BEAST"}),
            "other": ReceiverConfig(type="aoa"),
        }
    )
    pad = config.zones["pad"]
    pad.color = "#123456"
    pad.rules[0].color = "#abcdef"
    config.alert_colors = {"warn": "#ff0000"}
    zones = check(ZonesData, zones_payload(config))
    assert zones["zones"][0]["rules"][0]["when"]["altitude"] == {"max": 2000}
    app_config = check(AppConfig, app_config_payload(config))
    assert "beast_port" not in app_config
    config.web = WebConfig(beast_port=30105)
    assert check(AppConfig, app_config_payload(config))["beast_port"] == 30105
