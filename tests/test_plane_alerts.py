from __future__ import annotations

import time

from helpers import make_config
from pyaerial.calc.geo import build_polygons
from pyaerial.pipeline import PlaneCalculator
from pyaerial.constants import (
    STORE_ALT,
    STORE_LAT,
    STORE_LONG,
)
from pyaerial.models import AircraftInfo, Datum, Plane, TrackState


def _plane(lat: float = 35.725, lon: float = -78.695, t: float | None = None):
    if t is None:
        t = time.time()
    return Plane(
        info=AircraftInfo(icao="abc123", callsign="N123AB"),
        track=TrackState(first_packet=t - 5, last_update=t),
        received_data={
            STORE_LAT: [Datum(lat, t - 5), Datum(lat + 0.001, t)],
            STORE_LONG: [Datum(lon, t - 5), Datum(lon, t)],
            STORE_ALT: [Datum(400.0, t)],
        },
        calculated_data={},
    )


def test_while_active_starts_at_activation_time(monkeypatch):
    config = make_config()
    config.zones["pad"].rules[0].dwell_seconds = 1
    calc = PlaneCalculator(config, build_polygons(config.zones))
    try:
        t0 = time.time()
        m0 = time.monotonic()
        monkeypatch.setattr(time, "time", lambda: t0)
        monkeypatch.setattr(time, "monotonic", lambda: m0)
        plane = _plane(t=t0)
        calc.calculate_plane(plane)
        assert calc._alert_state == {}
        monkeypatch.setattr(time, "time", lambda: t0 + 1.5)
        monkeypatch.setattr(time, "monotonic", lambda: m0 + 1.5)
        calc.calculate_plane(plane)
        keys = [k for k in calc._alert_state if k[0] == "abc123"]
        assert keys, "expected an active alert after dwell"
        state = calc._alert_state[keys[0]]
        assert state["activated_at"] == t0 + 1.5
        assert state["last_periodic"] == m0 + 1.5
    finally:
        calc.close()


def test_forget_motion_drops_kalman():
    config = make_config()
    calc = PlaneCalculator(config, build_polygons(config.zones))
    try:
        plane = _plane()
        calc.calculate_plane(plane)
        assert "abc123" in calc._kalman_filters
        calc.forget_motion("abc123")
        assert "abc123" not in calc._kalman_filters
        assert "abc123" not in calc._smoothed_turn_rates
    finally:
        calc.close()


def test_dwell_holds_activation_when_hysteresis_is_zero():
    config = make_config()
    assert config.zones["pad"].rules[0].hysteresis_seconds == 0
    assert config.zones["pad"].rules[0].dwell_seconds == 60
    calc = PlaneCalculator(config, build_polygons(config.zones))
    try:
        plane = _plane()
        calc.calculate_plane(plane)
        assert calc._alert_state == {}
        assert any(key[0] == "abc123" for key in calc._pending_match)
    finally:
        calc.close()


def test_metadata_lookup_failure_is_retried():
    class _FailingDb:
        available = True

        def lookup_cached(self, icao: str):
            raise RuntimeError("hexdb down")

        def is_cached(self, icao: str) -> bool:
            return False

    config = make_config()
    calc = PlaneCalculator(
        config, build_polygons(config.zones), aircraft_db=_FailingDb()
    )
    try:
        plane = _plane()
        icao = plane.info.icao
        calc._bg_lookup_metadata(icao)
        assert plane.info.metadata_resolved is not True
        result = calc._pending_results.get(icao)
        assert result is not None
        assert result["resolved"] is False
    finally:
        calc.close()


def test_deactivate_clears_lookup_state_and_discards_inflight_result():
    class _FailingDb:
        available = True

        def lookup_cached(self, icao: str):
            raise RuntimeError("hexdb down")

        def is_cached(self, icao: str) -> bool:
            return False

    config = make_config()
    calc = PlaneCalculator(
        config, build_polygons(config.zones), aircraft_db=_FailingDb()
    )
    try:
        plane = _plane()
        icao = plane.info.icao
        calc._bg_lookup_metadata(icao)
        assert icao in calc._pending_results
        assert icao in calc._lookup_failures
        assert calc._lookup_backoff_until[icao] > time.monotonic()
        calc.deactivate_plane(plane)
        assert icao not in calc._pending_results
        assert icao not in calc._lookup_failures
        assert icao not in calc._lookup_backoff_until

        # A lookup still running when the plane expires must not leak a result.
        calc._pending_lookups.add(icao)
        calc.deactivate_plane(plane)
        calc._bg_lookup_metadata(icao)
        assert icao not in calc._pending_results
        assert icao not in calc._pending_lookups
        assert icao not in calc._cancelled_lookups
    finally:
        calc.close()


def test_hysteresis_delays_activation():
    from helpers import make_rule
    from pyaerial.config.schema import ZoneConfig

    config = make_config(
        zones={
            "pad": ZoneConfig(
                coordinates=[
                    [35.72, -78.70],
                    [35.73, -78.70],
                    [35.73, -78.69],
                    [35.72, -78.69],
                    [35.72, -78.70],
                ],
                rules=[
                    make_rule(
                        altitude={"max": 2000},
                        eta={"max": 120},
                        hysteresis_seconds=30,
                    )
                ],
            )
        }
    )
    calc = PlaneCalculator(config, build_polygons(config.zones))
    try:
        plane = _plane()
        calc.calculate_plane(plane)
        assert calc._alert_state == {}
        assert any(key[0] == "abc123" for key in calc._pending_match)
    finally:
        calc.close()


def test_deactivate_clears_motion_and_alerts():
    config = make_config()
    calc = PlaneCalculator(config, build_polygons(config.zones))
    try:
        plane = _plane()
        calc.calculate_plane(plane)
        calc.deactivate_plane(plane)
        assert plane.active_alerts == []
        assert "abc123" not in calc._kalman_filters
    finally:
        calc.close()


def test_one_bad_plane_does_not_stop_alerting_for_others(caplog):
    config = make_config()
    config.zones["pad"].rules[0].dwell_seconds = 1
    calc = PlaneCalculator(config, build_polygons(config.zones))
    real_check = calc.alerts.check

    def check(plane, *args):
        if plane.info.icao == "bad001":
            # e.g. shapely rejecting a non-finite projected point
            raise ValueError("Point coordinates must be finite")
        return real_check(plane, *args)

    calc.alerts.check = check  # type: ignore[method-assign]
    try:
        bad = _plane()
        bad.info.icao = "bad001"
        good = _plane()
        planes = {"bad001": bad, "abc123": good}
        with caplog.at_level("ERROR", logger="pyaerial.pipeline"):
            calc.calculate_all(planes)
            calc.calculate_all(planes)
        assert any(key[0] == "abc123" for key in calc._pending_match)
        # Logged with a traceback once, not every tick.
        assert sum("bad001" in r.getMessage() for r in caplog.records) == 1
    finally:
        calc.close()


def test_registration_reaches_plane_info_and_live_summary(fake):
    from pyaerial.store.live import LiveWriter

    class _Db:
        available = True

        def lookup_cached(self, icao: str):
            return {
                "registration": "HB-JZA",
                "model": "A320 214",
                "owner": "E2E Air",
                "country": "Switzerland",
                "typecode": "A320",
            }

        def is_cached(self, icao: str) -> bool:
            return True

    config = make_config()
    store = LiveWriter("redis://localhost:1/0")
    calc = PlaneCalculator(config, build_polygons(config.zones), aircraft_db=_Db())
    try:
        plane = _plane()
        calc._bg_lookup_metadata("abc123", "N123AB")
        calc.calculate_plane(plane)
        info = plane.info
        assert info.registration == "HB-JZA"
        assert info.aircraft_type == "A320"
        assert info.callsign == "N123AB"  # live callsign kept
        store.write_live_planes({"abc123": plane})
        (summary,) = store.get_flights()
        assert summary["registration"] == "HB-JZA"
    finally:
        calc.close()
        store.close()


def _enrichment_disabled_config():
    config = make_config()
    config.enrichment.enabled = False
    return config


class _CacheOnlyDb:
    """Fails the test if the online lookup path is taken."""

    available = True

    def __init__(self, cached: dict) -> None:
        self.cached = cached

    def lookup_cached(self, icao: str):
        raise AssertionError("online lookup with enrichment disabled")

    def is_cached(self, icao: str) -> bool:
        raise AssertionError("retry bookkeeping with enrichment disabled")

    def lookup_cached_fast(self, icao: str):
        return self.cached.get(icao)


def test_enrichment_disabled_reads_local_cache_only():
    db = _CacheOnlyDb({"abc123": {"registration": "N123AB", "typecode": "C172"}})
    config = _enrichment_disabled_config()
    calc = PlaneCalculator(config, build_polygons(config.zones), aircraft_db=db)
    try:
        calc._bg_lookup_metadata("abc123", "")
        calc._bg_lookup_metadata("def456", "")  # not cached: final, no backoff
        assert calc._pending_results["abc123"]["registration"] == "N123AB"
        assert calc._pending_results["abc123"]["resolved"] is True
        assert calc._pending_results["def456"]["resolved"] is True
        assert "def456" not in calc._lookup_backoff_until
    finally:
        calc.close()


def test_enrichment_enabled_by_default_uses_online_lookup():
    db = _CacheOnlyDb({})
    config = make_config()
    calc = PlaneCalculator(config, build_polygons(config.zones), aircraft_db=db)
    try:
        calc._bg_lookup_metadata("abc123", "")
        # The AssertionError from lookup_cached is caught as a failed lookup.
        assert calc._pending_results["abc123"]["resolved"] is False
    finally:
        calc.close()


class _SlowDb:
    available = True

    def __init__(self, delay: float):
        import threading

        self.delay = delay
        self.started = threading.Event()
        self.release = threading.Event()

    def lookup_cached(self, icao: str):
        self.started.set()
        self.release.wait(self.delay)
        return {"callsign": "LATE", "model": "X"}

    def is_cached(self, icao: str) -> bool:
        return True


def test_close_does_not_wait_for_inflight_lookup_and_drops_late_result():
    db = _SlowDb(delay=10.0)
    config = make_config()
    calc = PlaneCalculator(config, build_polygons(config.zones), aircraft_db=db)
    plane = _plane()
    calc._resolve_callsign(plane)
    assert db.started.wait(2.0)
    started = time.monotonic()
    calc.close()
    assert time.monotonic() - started < 1.0
    db.release.set()  # the lookup now finishes after close
    for thread in calc._lookup_threads:
        thread.join(2.0)
    assert calc._pending_results == {}
    # No new lookups are started once closed.
    other = _plane()
    other.info.icao = "def456"
    calc._resolve_callsign(other)
    assert "def456" not in calc._pending_lookups


def test_inflight_lookup_does_not_delay_interpreter_exit():
    """SIGTERM must not stall on an HTTP lookup (Docker kills after 10 s)."""
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    script = textwrap.dedent(
        """
        import sys, threading, time
        sys.path.insert(0, %r)
        from helpers import make_config, make_plane
        from pyaerial.calc.geo import build_polygons
        from pyaerial.pipeline import PlaneCalculator
        started = threading.Event()
        class Db:
            available = True
            def lookup_cached(self, icao):
                started.set()
                time.sleep(30)
            def is_cached(self, icao):
                return True
        config = make_config()
        calc = PlaneCalculator(config, build_polygons(config.zones), aircraft_db=Db())
        calc._resolve_callsign(make_plane("abc123"))
        started.wait(5)
        calc.close()
        """
    ) % str(Path(__file__).parent)
    started = time.monotonic()
    subprocess.run([sys.executable, "-c", script], check=True, timeout=25)
    assert time.monotonic() - started < 10.0


def test_single_position_still_calculates():
    config = make_config()
    calc = PlaneCalculator(config, build_polygons(config.zones))
    try:
        t = 1_700_000_000.0
        plane = Plane(
            info=AircraftInfo(icao="abc123"),
            track=TrackState(first_packet=t, last_update=t),
            received_data={
                STORE_LAT: [Datum(35.725, t)],
                STORE_LONG: [Datum(-78.695, t)],
            },
            calculated_data={},
        )
        calc.calculate_plane(plane)  # must not raise
    finally:
        calc.close()
