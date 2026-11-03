"""Alert notifications: one stable alert id on every hook, eager alerter
construction, and webhook secrets kept out of errors and logs."""

from __future__ import annotations

import http.server
import logging
import socketserver
import threading
import time

import pytest

from helpers import make_config
from pyaerial.alerters import Alerter, register_alerter
from pyaerial.alerters.webhook import WebhookAlerter
from pyaerial.alerts.engine import AlertEngine
from pyaerial.calc.geo import build_polygons
from pyaerial.calc.motion import ResolvedMotion
from pyaerial.config.schema import AlertActionConfig, WhileActiveConfig
from pyaerial.constants import (
    STORE_ALT,
)
from pyaerial.models import AircraftInfo, Datum, Plane, TrackState

_SEEN: list[dict] = []


@register_alerter("delivery_capture")
class _Capture(Alerter):
    def alert(self, meta: dict, payload: dict) -> None:
        _SEEN.append(meta)


@register_alerter("delivery_broken")
class _Broken(Alerter):
    def configure(self, arguments: dict) -> None:
        raise ValueError("missing api key")

    def alert(self, meta: dict, payload: dict) -> None:  # pragma: no cover
        raise AssertionError("never built")


class _Store:
    """Records what the engine hands the live store."""

    def __init__(self) -> None:
        self.episodes: list[dict] = []

    def record_alert_episode(self, plane, meta, payload, **kwargs) -> None:
        self.episodes.append({"meta": meta, **kwargs})

    def update_active_alert(self, plane, alert_id, meta, payload, now) -> None:
        pass


def _engine(actions: list[AlertActionConfig], store=None) -> AlertEngine:
    config = make_config()
    rule = config.zones["pad"].rules[0]
    rule.dwell_seconds = 1
    rule.on_activate = list(actions)
    rule.on_deactivate = list(actions)
    rule.while_active = WhileActiveConfig(interval_seconds=1, actions=list(actions))
    return AlertEngine(config, build_polygons(config.zones), store)


def _run_episode(engine: AlertEngine, monkeypatch) -> None:
    mono = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: mono[0])
    now = time.time()
    plane = Plane(
        info=AircraftInfo(icao="abc123"),
        track=TrackState(first_packet=1700000000.0, last_update=now),
        received_data={STORE_ALT: [Datum(400.0, now)]},
    )
    motion = ResolvedMotion(heading_deg=0.0, speed_kph=0.0, turn_rate_deg_s=0.0)
    for _ in range(4):  # hold, activate, while_active, while_active
        engine.check(plane, (35.725, -78.695), motion, "N1")
        mono[0] += 1.5
    engine.check(plane, (40.0, -70.0), motion, "N1")  # leaves: deactivate


def test_every_hook_carries_one_stable_alert_id(monkeypatch):
    _SEEN.clear()
    store = _Store()
    engine = _engine([AlertActionConfig(method="delivery_capture")], store)
    try:
        _run_episode(engine, monkeypatch)
    finally:
        monkeypatch.undo()
        engine.close()
    hooks = [m["reason"]["hook"] for m in _SEEN]
    assert hooks[0] == "activate" and hooks[-1] == "deactivate"
    assert "while_active" in hooks
    assert len({m["alert_id"] for m in _SEEN}) == 1
    assert all(m["flight_id"] == "abc123-1700000000" for m in _SEEN)
    assert all(m["icao"] == "abc123" for m in _SEEN)
    # The live store receives the same id it already stores the episode under.
    assert {e["alert_id"] for e in store.episodes} == {_SEEN[0]["alert_id"]}


def test_stored_alert_document_shape_is_unchanged():
    from pyaerial.store.live import LiveWriter

    plane = Plane(
        info=AircraftInfo(icao="abc123"),
        track=TrackState(first_packet=1.0, last_update=2.0),
    )
    meta = {
        "alert_id": "a1",
        "flight_id": "abc123-1",
        "icao": "abc123",
        "zone": "z",
        "type": "r",
    }
    doc = LiveWriter._alert_doc(
        None,
        plane,
        meta,
        {},
        alert_id="a1",
        activated_at=1.0,
        active=True,  # type: ignore[arg-type]
    )
    assert set(doc) == {
        "alert_id",
        "flight_id",
        "icao",
        "callsign",
        "zone",
        "rule",
        "active",
        "activated_at",
        "deactivated_at",
        "eta",
        "reason",
        "last_updated",
        "position",
        "altitude",
    }


class _Resp:
    status_code = 204
    headers: dict = {}

    def close(self) -> None:
        pass


@pytest.mark.parametrize("fmt", ["json", "discord", "slack"])
def test_webhook_bodies_include_alert_id(monkeypatch, fmt):
    alerter = WebhookAlerter({"url": "https://example.com/hook", "format": fmt})
    bodies: list[dict] = []
    monkeypatch.setattr(
        alerter._session,
        "request",
        lambda *a, **kw: bodies.append(kw["json"]) or _Resp(),
    )
    meta = {
        "alert_id": "abc123-1:pad:warn:ff00",
        "flight_id": "abc123-1",
        "icao": "abc123",
        "type": "warn",
        "zone": "pad",
        "reason": {"hook": "deactivate"},
    }
    alerter.alert(meta, {})
    body = bodies[0]
    if fmt == "json":
        assert body["alert_id"] == meta["alert_id"]
        assert body["flight_id"] == "abc123-1"
        assert body["icao"] == "abc123"
    elif fmt == "discord":
        assert body["embeds"][0]["footer"]["text"] == f"alert {meta['alert_id']}"
    else:
        context = [b for b in body["blocks"] if b["type"] == "context"]
        assert context[0]["elements"][0]["text"] == f"alert {meta['alert_id']}"


def test_broken_alerter_is_reported_at_startup_and_does_not_block_others(
    monkeypatch, caplog
):
    _SEEN.clear()
    actions = [
        AlertActionConfig(method="delivery_broken", options={"key": "x"}),
        AlertActionConfig(method="delivery_capture"),
    ]
    with caplog.at_level(logging.ERROR, logger="pyaerial.alerts"):
        engine = _engine(actions)
    startup = [r.getMessage() for r in caplog.records]
    assert len(startup) == 1
    assert "zones.pad.rules.warn" in startup[0]
    assert "delivery_broken" in startup[0] and "missing api key" in startup[0]
    caplog.clear()
    try:
        with caplog.at_level(logging.ERROR, logger="pyaerial.alerts"):
            _run_episode(engine, monkeypatch)
    finally:
        monkeypatch.undo()
        engine.close()
    assert [m["reason"]["hook"] for m in _SEEN][0] == "activate"
    assert not caplog.records  # not retried / re-logged per alert


def test_webhook_alerter_built_eagerly_with_redacted_error(caplog):
    action = AlertActionConfig(
        method="webhook",
        options={"url": "https://example.com/api/webhooks/1/TOKEN", "format": "bogus"},
    )
    with caplog.at_level(logging.ERROR, logger="pyaerial.alerts"):
        engine = _engine([action])
    engine.close()
    assert "format" in caplog.text
    assert "TOKEN" not in caplog.text


def test_urllib3_debug_lines_do_not_leak_webhook_token(caplog):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    # Not HTTPServer: its server_bind does a reverse-DNS getfqdn() lookup.
    server = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    urllib3_log = logging.getLogger("urllib3")
    previous_level = urllib3_log.level
    try:
        url = (
            f"http://127.0.0.1:{server.server_address[1]}"
            "/api/webhooks/123/SECRET-TOKEN?thread_id=9&token=QS-SECRET"
        )
        alerter = WebhookAlerter({"url": url})
        with caplog.at_level(logging.DEBUG):
            urllib3_log.setLevel(logging.DEBUG)
            alerter.alert({"icao": "abc123", "type": "warn", "zone": "pad"}, {})
        alerter.close()
    finally:
        urllib3_log.setLevel(previous_level)
        server.shutdown()
        server.server_close()
    urllib3_lines = [
        r.getMessage() for r in caplog.records if r.name.startswith("urllib3")
    ]
    assert any("POST /<redacted>" in line for line in urllib3_lines), urllib3_lines
    assert "SECRET" not in caplog.text
