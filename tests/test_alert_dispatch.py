from __future__ import annotations

import threading
import time

from pyaerial.alerters import Alerter, register_alerter
from pyaerial.alerts.dispatch import AlertDispatcher
from pyaerial.alerts.engine import AlertEngine
from pyaerial.calc.geo import build_polygons
from pyaerial.config.schema import AlertActionConfig
from pyaerial.constants import (
    STORE_FIRST_PACKET,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_MOST_RECENT_PACKET,
    STORE_RECV_DATA,
)
from pyaerial.models import Datum
from helpers import make_config


def test_dispatcher_preserves_order_per_key():
    seen: dict[str, list[int]] = {}
    lock = threading.Lock()

    def record(key: str, n: int) -> None:
        # Jitter so out-of-order delivery would show up across workers.
        time.sleep(0.0005 * (n % 3))
        with lock:
            seen.setdefault(key, []).append(n)

    dispatcher = AlertDispatcher(workers=4, max_pending_per_worker=1000)
    for n in range(50):
        for key in ("a", "b", "c", "d", "e"):
            assert dispatcher.submit(key, record, key, n)
    dispatcher.close(timeout=10.0)
    assert all(values == list(range(50)) for values in seen.values())
    assert len(seen) == 5


def test_dispatcher_is_bounded_and_drops_when_full(caplog):
    gate = threading.Event()
    dispatcher = AlertDispatcher(workers=1, max_pending_per_worker=2)
    try:
        assert dispatcher.submit("k", gate.wait)  # occupies the worker
        time.sleep(0.05)
        assert dispatcher.submit("k", lambda: None)
        assert dispatcher.submit("k", lambda: None)
        with caplog.at_level("WARNING", logger="pyaerial.alerts"):
            assert dispatcher.submit("k", lambda: None) is False
        assert dispatcher.dropped == 1
        assert "Dropped alert notification" in caplog.text
    finally:
        gate.set()
        dispatcher.close(timeout=1.0)


def test_dispatcher_close_does_not_wait_for_whole_backlog():
    ran: list[int] = []

    def slow(n: int) -> None:
        time.sleep(0.05)
        ran.append(n)

    dispatcher = AlertDispatcher(workers=1, max_pending_per_worker=500)
    for n in range(200):
        dispatcher.submit("k", slow, n)
    started = time.monotonic()
    dispatcher.close(timeout=0.2)
    assert time.monotonic() - started < 1.0
    assert len(ran) < 200
    assert dispatcher.submit("k", slow, 999) is False


_CAPTURED: list[tuple[dict, dict]] = []


@register_alerter("capture_test")
class _CaptureAlerter(Alerter):
    def alert(self, meta: dict, payload: dict) -> None:
        _CAPTURED.append((meta, payload))


def test_deactivate_without_position_reports_none_not_null_island():
    _CAPTURED.clear()
    config = make_config()
    rule = config.zones["pad"].rules[0]
    rule.on_deactivate = [AlertActionConfig(method="capture_test", options={})]
    engine = AlertEngine(config, build_polygons(config.zones))
    try:
        plane = {
            STORE_INFO: {STORE_ICAO: "abc123"},
            STORE_RECV_DATA: {},
            STORE_INTERNAL: {STORE_FIRST_PACKET: 1.0, STORE_MOST_RECENT_PACKET: 2.0},
        }
        engine._alert_state[("abc123", "pad", rule.name)] = {
            "activated_at": 1.0,
            "last_periodic": 0.0,
            "alert_id": "x",
            "eta": 5.0,
        }
        engine.deactivate(plane)
    finally:
        engine.close()
    assert len(_CAPTURED) == 1
    _meta, payload = _CAPTURED[0]
    assert payload[STORE_LAT] is None
    assert payload["longitude"] is None


def test_activation_hold_uses_monotonic_clock(monkeypatch):
    """A wall-clock jump must not satisfy the dwell hold."""
    from pyaerial.calc.motion import ResolvedMotion

    config = make_config()
    config.zones["pad"].rules[0].dwell_seconds = 30
    engine = AlertEngine(config, build_polygons(config.zones))
    wall = [time.time()]
    mono = [1000.0]
    monkeypatch.setattr(time, "time", lambda: wall[0])
    monkeypatch.setattr(time, "monotonic", lambda: mono[0])
    plane = {
        STORE_INFO: {STORE_ICAO: "abc123"},
        STORE_RECV_DATA: {"altitude": [Datum(400.0, wall[0])]},
        STORE_INTERNAL: {STORE_FIRST_PACKET: wall[0], STORE_MOST_RECENT_PACKET: wall[0]},
    }
    motion = ResolvedMotion(heading_deg=0.0, speed_kph=0.0, turn_rate_deg_s=0.0)
    position = (35.725, -78.695)
    try:
        engine.check(plane, position, motion, "")
        assert engine._alert_state == {}
        wall[0] += 3600.0  # NTP step forward; monotonic barely moved
        mono[0] += 1.0
        engine.check(plane, position, motion, "")
        assert engine._alert_state == {}
        mono[0] += 30.0
        engine.check(plane, position, motion, "")
        assert engine._alert_state
    finally:
        monkeypatch.undo()
        engine.close()
