from __future__ import annotations

from shapely import Polygon

from helpers import make_config, make_rule
from pyaerial.alerts.retain import should_retain
from pyaerial.calc.geo import build_polygons
from pyaerial.config.schema import ArchiveConfig, ZoneConfig
from pyaerial.constants import (
    STORE_ALT,
    STORE_CALC_DATA,
    STORE_FIRST_PACKET,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTEGRITY_LOG,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_MOST_RECENT_PACKET,
    STORE_RECV_DATA,
)
from pyaerial.models import Datum, Verdict
from pyaerial.store.history import HistoryStore

RULES_ONLY = ArchiveConfig(flights="rules")


def _plane():
    return {
        STORE_INFO: {STORE_ICAO: "abc123"},
        STORE_RECV_DATA: {
            STORE_LAT: [Datum(35.725, 1.0)],
            STORE_LONG: [Datum(-78.695, 1.0)],
        },
        STORE_CALC_DATA: {
            STORE_HEADING: [Datum(0.0, 1.0)],
            STORE_HORIZ_SPEED: [Datum(0.0, 1.0)],
        },
        STORE_INTERNAL: {
            STORE_FIRST_PACKET: 1.0,
            STORE_MOST_RECENT_PACKET: 10.0,
        },
    }


def test_retain_false_is_honored_even_with_alerts():
    config = make_config(
        archive=RULES_ONLY,
        zones={
            "pad": ZoneConfig(
                coordinates=[
                    [35.72, -78.70],
                    [35.73, -78.70],
                    [35.73, -78.69],
                    [35.72, -78.69],
                ],
                rules=[make_rule(name="warn", retain=False, dwell_seconds=1)],
            )
        },
    )
    alerts = [
        {
            "zone": "pad",
            "rule": "warn",
            "activated_at": 1.0,
            "deactivated_at": 100.0,
        }
    ]
    assert should_retain(_plane(), alerts, config, {}, positions=1) is False


def test_retain_true_keeps_any_fired_episode():
    """Regression: an alert that fired (activation hold already met by the
    engine) used to be dropped when its episode was shorter than dwell."""
    config = make_config(
        archive=RULES_ONLY,
        zones={
            "pad": ZoneConfig(
                coordinates=[
                    [35.72, -78.70],
                    [35.73, -78.70],
                    [35.73, -78.69],
                    [35.72, -78.69],
                ],
                rules=[make_rule(name="warn", retain=True, dwell_seconds=60)],
            )
        },
    )
    short = [
        {
            "zone": "pad",
            "rule": "warn",
            "activated_at": 1.0,
            "deactivated_at": 10.0,
        }
    ]
    long = [
        {
            "zone": "pad",
            "rule": "warn",
            "activated_at": 1.0,
            "deactivated_at": 80.0,
        }
    ]
    assert should_retain(_plane(), short, config, {}, positions=1) is True
    assert should_retain(_plane(), long, config, {}, positions=1) is True
    never_fired = [{"zone": "pad", "rule": "warn", "activated_at": None}]
    assert (
        should_retain(_plane(), never_fired, config, {}, positions=1)
        is False
    )
    unknown_rule = [{"zone": "pad", "rule": "gone", "activated_at": 1.0}]
    assert (
        should_retain(_plane(), unknown_rule, config, {}, positions=1)
        is False
    )


def test_retain_fallback_uses_matching_seconds_not_sample_count():
    from pyaerial.calc.geo import build_polygons

    config = make_config(
        archive=RULES_ONLY,
        zones={
            "pad": ZoneConfig(
                coordinates=[
                    [35.72, -78.70],
                    [35.73, -78.70],
                    [35.73, -78.69],
                    [35.72, -78.69],
                ],
                rules=[make_rule(name="warn", retain=True, dwell_seconds=60)],
            )
        },
    )
    polygons = build_polygons(config.zones)
    plane = _plane()
    plane[STORE_RECV_DATA][STORE_LAT] = [Datum(35.725, 1.0), Datum(35.725, 70.0)]
    plane[STORE_RECV_DATA][STORE_LONG] = [Datum(-78.695, 1.0), Datum(-78.695, 70.0)]
    plane[STORE_RECV_DATA][STORE_ALT] = [Datum(300.0, 1.0), Datum(300.0, 70.0)]
    plane[STORE_CALC_DATA][STORE_HEADING] = [Datum(0.0, 1.0), Datum(0.0, 70.0)]
    plane[STORE_CALC_DATA][STORE_HORIZ_SPEED] = [Datum(0.0, 1.0), Datum(0.0, 70.0)]
    plane[STORE_INTERNAL][STORE_FIRST_PACKET] = 1.0
    plane[STORE_INTERNAL][STORE_MOST_RECENT_PACKET] = 70.0
    assert should_retain(plane, [], config, polygons, positions=1) is True


def test_retain_does_not_credit_whole_flight_from_one_match():
    from pyaerial.calc.geo import build_polygons

    config = make_config(
        archive=RULES_ONLY,
        zones={
            "pad": ZoneConfig(
                coordinates=[
                    [35.72, -78.70],
                    [35.73, -78.70],
                    [35.73, -78.69],
                    [35.72, -78.69],
                ],
                rules=[
                    make_rule(
                        name="warn",
                        retain=True,
                        dwell_seconds=60,
                        eta={"max": 0},
                    )
                ],
            )
        },
    )
    polygons = build_polygons(config.zones)
    plane = _plane()
    plane[STORE_RECV_DATA][STORE_LAT] = [
        Datum(35.0, 1.0),
        Datum(35.725, 70.0),
    ]
    plane[STORE_RECV_DATA][STORE_LONG] = [
        Datum(-78.0, 1.0),
        Datum(-78.695, 70.0),
    ]
    plane[STORE_RECV_DATA][STORE_ALT] = [Datum(300.0, 1.0), Datum(300.0, 70.0)]
    plane[STORE_CALC_DATA][STORE_HEADING] = [Datum(0.0, 1.0), Datum(0.0, 70.0)]
    plane[STORE_CALC_DATA][STORE_HORIZ_SPEED] = [Datum(0.0, 1.0), Datum(0.0, 70.0)]
    plane[STORE_INTERNAL][STORE_FIRST_PACKET] = 1.0
    plane[STORE_INTERNAL][STORE_MOST_RECENT_PACKET] = 70.0
    assert should_retain(plane, [], config, polygons, positions=1) is False


def test_retain_fallback_requires_continuous_dwell():
    """Two 40 s matches split by a miss are not a 60 s dwell (live engine agrees)."""
    from pyaerial.calc.geo import build_polygons

    config = make_config(
        archive=RULES_ONLY,
        zones={
            "pad": ZoneConfig(
                coordinates=[
                    [35.72, -78.70],
                    [35.73, -78.70],
                    [35.73, -78.69],
                    [35.72, -78.69],
                ],
                rules=[
                    make_rule(
                        name="warn",
                        dwell_seconds=60,
                        altitude={"max": 1000},
                        distance={"max": 0},
                    )
                ],
            )
        },
    )
    polygons = build_polygons(config.zones)

    def plane_with(alts: list[float], times: list[float], last: float) -> dict:
        plane = _plane()
        plane[STORE_RECV_DATA][STORE_LAT] = [Datum(35.725, t) for t in times]
        plane[STORE_RECV_DATA][STORE_LONG] = [Datum(-78.695, t) for t in times]
        plane[STORE_RECV_DATA][STORE_ALT] = [
            Datum(a, t) for a, t in zip(alts, times, strict=True)
        ]
        plane[STORE_CALC_DATA][STORE_HEADING] = [Datum(0.0, t) for t in times]
        plane[STORE_CALC_DATA][STORE_HORIZ_SPEED] = [Datum(0.0, t) for t in times]
        plane[STORE_INTERNAL][STORE_FIRST_PACKET] = times[0]
        plane[STORE_INTERNAL][STORE_MOST_RECENT_PACKET] = last
        return plane

    split = plane_with([300, 300, 5000, 300], [0, 40, 50, 90], last=120)
    assert should_retain(split, [], config, polygons, positions=4) is False
    unbroken = plane_with([5000, 300, 300, 300], [0, 10, 40, 60], last=75)
    assert should_retain(unbroken, [], config, polygons, positions=4) is True


def test_archive_all_keeps_flights_without_any_rule_match():
    config = make_config(zones={}, archive=ArchiveConfig(min_positions=2))
    assert should_retain(_plane(), [], config, {}, positions=2) is True
    assert should_retain(_plane(), [], config, {}, positions=1) is False


def test_archive_all_still_keeps_retain_rule_alerts_below_min_positions():
    config = make_config(
        zones={
            "pad": ZoneConfig(
                coordinates=[
                    [35.72, -78.70],
                    [35.73, -78.70],
                    [35.73, -78.69],
                    [35.72, -78.69],
                ],
                rules=[make_rule(name="warn", retain=True, dwell_seconds=60)],
            )
        },
        archive=ArchiveConfig(min_positions=5),
    )
    fired = [{"zone": "pad", "rule": "warn", "activated_at": 1.0}]
    assert should_retain(_plane(), fired, config, {}, positions=1) is True


def test_disabled_store_finalize_succeeds_without_sqlite():
    store = HistoryStore(":memory:", disabled=True)
    assert store.finalize_plane(_plane(), alerts=[]) is True


# --- Retain uses the verdict in effect at each sample ------------------------


_PAD = Polygon([(35.72, -78.70), (35.73, -78.70), (35.73, -78.69), (35.72, -78.69)])


_IN_PAD = (35.725, -78.695)


def _retain_plane(log: list[Verdict], final: dict) -> dict:
    times = [0.0, 30.0, 60.0, 90.0]
    return {
        STORE_INFO: {STORE_ICAO: "abc123", "integrity": final},
        STORE_RECV_DATA: {
            STORE_LAT: [Datum(_IN_PAD[0], t) for t in times],
            STORE_LONG: [Datum(_IN_PAD[1], t) for t in times],
        },
        STORE_CALC_DATA: {
            STORE_HEADING: [Datum(0.0, t) for t in times],
            STORE_HORIZ_SPEED: [Datum(0.0, t) for t in times],
        },
        STORE_INTERNAL: {
            STORE_FIRST_PACKET: 0.0,
            STORE_MOST_RECENT_PACKET: 95.0,
            STORE_INTEGRITY_LOG: log,
        },
    }


def _retain_config(**when):
    config = make_config(
        archive=ArchiveConfig(flights="rules"),
        zones={
            "pad": ZoneConfig(
                coordinates=[[lat, lon] for lat, lon in _PAD.exterior.coords],
                rules=[make_rule("r", dwell_seconds=60, distance={"max": 0}, **when)],
            )
        },
    )
    return config, build_polygons(config.zones)


def test_retain_uses_verdict_in_effect_at_each_sample():
    """Spoofed for the whole stay, recovered at the very end: still retained."""
    config, polygons = _retain_config(integrity={"max": 0.5})
    log = [
        Verdict(-1.0, 0.3, "spoofed", ("tdoa",), {}),
        Verdict(91.0, 0.95, "ok", (), {}),
    ]
    plane = _retain_plane(log, {"score": 0.95, "status": "ok"})
    assert should_retain(plane, [], config, polygons, positions=4) is True


def test_retain_does_not_credit_unknown_verdicts():
    config, polygons = _retain_config(integrity={"min": 0.8})
    unknown = [Verdict(-1.0, 1.0, "unknown", (), {})]
    plane = _retain_plane(unknown, {"score": 1.0, "status": "unknown"})
    assert should_retain(plane, [], config, polygons, positions=4) is False
    # And samples before the first verdict carry none.
    late_ok = [Verdict(80.0, 0.95, "ok", (), {})]
    plane = _retain_plane(late_ok, {"score": 0.95, "status": "ok"})
    assert should_retain(plane, [], config, polygons, positions=4) is False
