from __future__ import annotations

import math

from shapely import Polygon

from pyaerial.calc import evaluate
from pyaerial.config.schema import FieldConstraint, RuleConfig
from pyaerial.constants import STORE_CALC_DATA, STORE_HEADING, STORE_HORIZ_SPEED
from pyaerial.models import Datum


def test_horizontal_speed_and_direction_aliases():
    plane = {
        STORE_CALC_DATA: {
            STORE_HORIZ_SPEED: [Datum(180.0, 1.0)],
            STORE_HEADING: [Datum(10.0, 1.0)],
        }
    }
    polygon = Polygon([(35.72, -78.70), (35.73, -78.70), (35.73, -78.69)])
    resolver = evaluate.make_resolver(plane, eta=30.0, polygon=polygon, position=(35.72, -78.70))
    assert resolver("horizontal_speed") == 180.0
    assert resolver("direction") == 10.0
    assert resolver("speed") == 180.0
    assert resolver("heading") == 10.0


def test_when_passes_heading_wraps_across_north():
    spec = {"heading": FieldConstraint(minimum=350, maximum=10)}
    assert evaluate.when_passes(spec, lambda _f: 5.0)
    assert evaluate.when_passes(spec, lambda _f: 355.0)
    assert not evaluate.when_passes(spec, lambda _f: 180.0)


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


def test_distance_is_km_proximity_is_metres():
    # A point well south of a 1-degree-ish box; exact km is not asserted,
    # only the 1000x relationship between the two fields.
    polygon = Polygon([(35.72, -78.70), (35.73, -78.70), (35.73, -78.69), (35.72, -78.69)])
    plane: dict = {}
    far = (34.0, -78.70)
    resolver = evaluate.make_resolver(plane, eta=math.inf, polygon=polygon, position=far)
    dist = resolver("distance")
    prox = resolver("proximity")
    assert dist is not None and prox is not None
    assert prox == dist * 1000
    assert dist > 1.0
