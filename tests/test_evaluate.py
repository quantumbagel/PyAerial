from __future__ import annotations

import math

from shapely import Polygon

from helpers import make_config, make_plane
from pyaerial.alerts.engine import AlertEngine
from pyaerial.calc import evaluate
from pyaerial.calc.geo import build_polygons
from pyaerial.config.schema import (
    FieldConstraint,
    RuleConfig,
)
from pyaerial.constants import (
    STORE_ALT,
    STORE_GNSS_HEIGHT,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_VERT_SPEED,
)
from pyaerial.models import AircraftInfo, Datum, Plane, TrackState


def test_horizontal_speed_and_direction_aliases():
    plane = make_plane(
        calc={
            STORE_HORIZ_SPEED: [Datum(180.0, 1.0)],
            STORE_HEADING: [Datum(10.0, 1.0)],
        }
    )
    polygon = Polygon([(35.72, -78.70), (35.73, -78.70), (35.73, -78.69)])
    resolver = evaluate.make_resolver(
        plane, eta=30.0, polygon=polygon, position=(35.72, -78.70)
    )
    assert resolver("horizontal_speed") == 180.0
    assert resolver("direction") == 10.0
    assert resolver("speed") == 180.0
    assert resolver("heading") == 10.0


def test_when_passes_heading_wraps_across_north():
    spec = {"heading": FieldConstraint(minimum=350, maximum=10)}
    assert evaluate.when_passes(spec, lambda _f: 5.0)
    assert evaluate.when_passes(spec, lambda _f: 355.0)
    assert not evaluate.when_passes(spec, lambda _f: 180.0)


def test_when_passes_heading_full_circle():
    spec = {"heading": FieldConstraint(minimum=0, maximum=360)}
    assert evaluate.when_passes(spec, lambda _f: 0.0)
    assert evaluate.when_passes(spec, lambda _f: 180.0)
    assert evaluate.when_passes(spec, lambda _f: 359.9)


def test_when_passes_missing_value_fails():
    spec = {"altitude": FieldConstraint(maximum=1000)}
    assert not evaluate.when_passes(spec, lambda _f: None)


def test_rule_config_rejects_unknown_when_field():
    try:
        RuleConfig(
            name="bad",
            when={"not_a_field": FieldConstraint(maximum=1)},
            dwell_seconds=1,
        )
    except Exception as exc:
        assert "unknown when field" in str(exc).lower() or "not_a_field" in str(exc)
    else:
        raise AssertionError("expected validation error")


def test_predicted_resolver_projects_latitude_and_longitude():
    """predict_seconds must move lat/lon too, not just distance and eta."""
    from pyaerial.constants import STORE_LAT, STORE_LONG

    plane = make_plane(
        recv={
            STORE_LAT: [Datum(35.0, 1.0)],
            STORE_LONG: [Datum(-78.0, 1.0)],
        }
    )
    polygon = Polygon([(36.0, -78.1), (36.1, -78.1), (36.1, -77.9)])
    # Due north at 900 km/h for 60 s: 15 km, about 0.135 degrees of latitude.
    resolver = evaluate.make_predicted_resolver(
        plane, polygon, (35.0, -78.0), 0.0, 900.0, 0.0, 60.0
    )
    assert abs(resolver("latitude") - 35.135) < 0.005
    assert abs(resolver("lon") - -78.0) < 1e-6
    spec = {
        "latitude": FieldConstraint(minimum=35.1),
        "eta": FieldConstraint(minimum=0),
    }
    assert evaluate.when_passes(spec, resolver)


def test_live_resolver_lat_lon_match_evaluated_position():
    from pyaerial.constants import STORE_LAT, STORE_LONG

    plane = make_plane(
        recv={
            STORE_LAT: [Datum(35.0, 1.0)],
            STORE_LONG: [Datum(-78.0, 1.0)],
        }
    )
    polygon = Polygon([(36.0, -78.1), (36.1, -78.1), (36.1, -77.9)])
    coasted = (35.05, -78.01)
    resolver = evaluate.make_resolver(
        plane, eta=math.inf, polygon=polygon, position=coasted
    )
    assert resolver("lat") == 35.05
    assert resolver("longitude") == -78.01


def test_distance_is_km_proximity_is_metres():
    # A point well south of a 1-degree-ish box; exact km is not asserted,
    # only the 1000x relationship between the two fields.
    polygon = Polygon(
        [(35.72, -78.70), (35.73, -78.70), (35.73, -78.69), (35.72, -78.69)]
    )
    plane = make_plane()
    far = (34.0, -78.70)
    resolver = evaluate.make_resolver(
        plane, eta=math.inf, polygon=polygon, position=far
    )
    dist = resolver("distance")
    prox = resolver("proximity")
    assert dist is not None and prox is not None
    assert prox == dist * 1000
    assert dist > 1.0


# --- altitude rules: baro altitude, else GNSS height -------------------------


_PAD = Polygon([(35.72, -78.70), (35.73, -78.70), (35.73, -78.69), (35.72, -78.69)])


_IN_PAD = (35.725, -78.695)


_LOW = {
    "altitude": FieldConstraint(maximum=1000),
    "distance": FieldConstraint(maximum=1),
}


def _altitude_plane(**recv: list[Datum]) -> Plane:
    return Plane(
        info=AircraftInfo(icao="abc123"),
        track=TrackState(first_packet=1.0, last_update=2.0),
        received_data=dict(recv),
    )


def test_gnss_only_aircraft_satisfies_altitude_rule():
    plane = _altitude_plane(**{STORE_GNSS_HEIGHT: [Datum(600.0, 1.0)]})
    resolver = evaluate.make_resolver(plane, 0.0, _PAD, _IN_PAD)
    assert resolver("altitude") == 600.0
    assert resolver("alt") == 600.0
    assert evaluate.when_passes(_LOW, resolver)
    assert evaluate.altitude_m(plane) == (600.0, "gnss")


def test_baro_is_preferred_over_gnss_when_present():
    # Baro is older than the GNSS sample, but still wins.
    plane = _altitude_plane(
        **{STORE_ALT: [Datum(3000.0, 1.0)], STORE_GNSS_HEIGHT: [Datum(600.0, 2.0)]}
    )
    resolver = evaluate.make_resolver(plane, 0.0, _PAD, _IN_PAD)
    assert resolver("altitude") == 3000.0
    assert not evaluate.when_passes(_LOW, resolver)
    assert evaluate.altitude_m(plane) == (3000.0, "baro")


def test_no_altitude_of_either_kind_fails_the_rule():
    resolver = evaluate.make_resolver(_altitude_plane(), 0.0, _PAD, _IN_PAD)
    assert resolver("altitude") is None
    assert not evaluate.when_passes(_LOW, resolver)


def test_retain_time_lookup_uses_gnss_fallback():
    plane = _altitude_plane(**{STORE_GNSS_HEIGHT: [Datum(100.0, 1.0), Datum(900.0, 50.0)]})
    resolver = evaluate.make_resolver(plane, 0.0, _PAD, _IN_PAD, at_time=2.0)
    assert resolver("altitude") == 100.0


def test_predicted_altitude_extrapolates_gnss_height():
    plane = _altitude_plane(
        **{
            STORE_GNSS_HEIGHT: [Datum(600.0, 1.0)],
            STORE_VERT_SPEED: [Datum(-5.0, 1.0)],
        }
    )
    resolver = evaluate.make_predicted_resolver(
        plane, _PAD, _IN_PAD, 0.0, 0.0, 0.0, 10.0
    )
    assert math.isclose(resolver("altitude"), 550.0)
    baro = _altitude_plane(
        **{STORE_ALT: [Datum(2000.0, 1.0)], STORE_GNSS_HEIGHT: [Datum(600.0, 1.0)]}
    )
    resolver = evaluate.make_predicted_resolver(
        baro, _PAD, _IN_PAD, 0.0, 0.0, 0.0, 10.0
    )
    assert resolver("altitude") == 2000.0


def test_alert_payload_reports_altitude_and_source():
    config = make_config()
    engine = AlertEngine(config, build_polygons(config.zones))
    try:
        gnss = engine._build_payload(
            _altitude_plane(**{STORE_GNSS_HEIGHT: [Datum(600.0, 1.0)]}), _IN_PAD
        )
        assert gnss[STORE_ALT] == 600.0 and gnss["altitude_source"] == "gnss"
        both = _altitude_plane(
            **{STORE_ALT: [Datum(300.0, 1.0)], STORE_GNSS_HEIGHT: [Datum(600.0, 1.0)]}
        )
        baro = engine._build_payload(both, _IN_PAD)
        assert baro[STORE_ALT] == 300.0 and baro["altitude_source"] == "baro"
        none = engine._build_payload(_altitude_plane(), None)
        assert none[STORE_ALT] is None and none["altitude_source"] is None
    finally:
        engine.close()


# --- integrity constraints need evidence -------------------------------------
# An unknown verdict never satisfies an integrity constraint.


_TRUSTED = {
    "integrity": FieldConstraint(minimum=0.8),
    "distance": FieldConstraint(maximum=1),
}


def _resolve(doc: dict | None):
    plane = make_plane(integrity=doc)
    return evaluate.make_resolver(plane, 0.0, _PAD, _IN_PAD)


def test_unknown_or_missing_status_does_not_satisfy_integrity():
    for doc in (
        None,
        {"score": 1.0, "status": "unknown", "checks": {}},
        {"score": 1.0},  # no status: no evidence either
        {"score": 1.0, "status": ""},
    ):
        resolver = _resolve(doc)
        assert resolver("integrity") is None, doc
        assert not evaluate.when_passes(_TRUSTED, resolver), doc
        # max-only constraints must not match unchecked aircraft either.
        spoof = {
            "integrity": FieldConstraint(maximum=1.0),
            "distance": FieldConstraint(maximum=1),
        }
        assert not evaluate.when_passes(spoof, resolver), doc


def test_known_status_uses_published_score():
    assert evaluate.when_passes(_TRUSTED, _resolve({"score": 0.9, "status": "ok"}))
    resolver = _resolve({"score": 0.6, "status": "suspect"})
    assert resolver("integrity") == 0.6
    assert not evaluate.when_passes(_TRUSTED, resolver)


def test_predicted_resolver_also_ignores_unknown():
    plane = make_plane(integrity={"score": 1.0, "status": "unknown"})
    resolver = evaluate.make_predicted_resolver(
        plane, _PAD, _IN_PAD, 0.0, 0.0, 0.0, 10.0
    )
    assert resolver("integrity") is None
