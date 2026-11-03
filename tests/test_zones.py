"""Zone geometry: polygon building and the zone/rule checks made when the
config loads."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from helpers import make_rule
from pyaerial.calc import geo
from pyaerial.config.loader import ConfigError, load_config
from pyaerial.config.schema import ZoneConfig

# --- Polygon building --------------------------------------------------------


@pytest.mark.parametrize(
    "ring, reason",
    [
        ([[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]], "Self-intersection"),
        ([[0, 0], [1, 1], [2, 2], [0, 0]], "Self-intersection"),
    ],
)
def test_build_polygons_rejects_invalid_zones(ring, reason):
    zone = ZoneConfig.model_construct(coordinates=ring, rules=[])
    with pytest.raises(ValueError, match=rf"zone 'bad': {reason}"):
        geo.build_polygons({"bad": zone})


def test_build_polygons_accepts_a_valid_zone():
    zone = ZoneConfig.model_construct(
        coordinates=[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]], rules=[]
    )
    assert geo.build_polygons({"ok": zone})["ok"].area == pytest.approx(1.0)


# --- Zone geometry and rule names at config load -----------------------------


_SQUARE = [[35.72, -78.70], [35.73, -78.70], [35.73, -78.69], [35.72, -78.69]]


# Edges 1-2 and 3-4 cross: a figure-eight, not a polygon.
_BOWTIE = [[35.72, -78.70], [35.73, -78.69], [35.72, -78.69], [35.73, -78.70]]


_YAML = """
home: {latitude: 35.7275, longitude: -78.6959}
receivers: {main: {type: dump1090}}
zones:
  approach:
    coordinates: %s
    rules:
      - name: warn
        when: {eta: {max: 120}}
        dwell_seconds: 1
"""


def test_rule_names_are_unique_per_zone():
    with pytest.raises(ValidationError, match="duplicate rule name.*warn"):
        ZoneConfig(coordinates=_SQUARE, rules=[make_rule("warn"), make_rule("warn")])
    zone = ZoneConfig(
        coordinates=_SQUARE, rules=[make_rule("warn"), make_rule("alert")]
    )
    assert [rule.name for rule in zone.rules] == ["warn", "alert"]


@pytest.mark.parametrize(
    "ring",
    [
        [[35.72, -78.70], [35.73, -78.70], [35.72, -78.70]],  # closing point counted
        [[35.72, -78.70], [35.73, -78.70], [35.73, -78.70], [35.72, -78.70]],
    ],
)
def test_zone_needs_three_distinct_vertices(ring):
    with pytest.raises(ValidationError, match="3 distinct"):
        ZoneConfig(coordinates=ring, rules=[make_rule()])


def test_closed_and_open_rings_are_both_accepted():
    assert ZoneConfig(coordinates=_SQUARE, rules=[make_rule()])
    assert ZoneConfig(coordinates=[*_SQUARE, _SQUARE[0]], rules=[make_rule()])


@pytest.mark.parametrize(
    ("ring", "reason"),
    [
        (_BOWTIE, "self-intersect"),
        ([[35.72, -78.70], [35.73, -78.70], [35.74, -78.70]], "area"),  # collinear
    ],
)
def test_invalid_polygon_is_a_config_error_naming_the_zone(tmp_path, ring, reason):
    path = tmp_path / "config.yaml"
    path.write_text(_YAML % ring)
    with pytest.raises(ConfigError, match=reason) as excinfo:
        load_config(path)
    assert "zones.approach" in str(excinfo.value)


def test_build_polygons_errors_are_reported_per_zone(tmp_path, monkeypatch):
    """Whatever build_polygons rejects becomes a config error, not a traceback."""
    from pyaerial.calc import geo

    def _reject(zones):
        raise ValueError("zone 'approach' is not a valid polygon: test")

    monkeypatch.setattr(geo, "build_polygons", _reject)
    path = tmp_path / "config.yaml"
    path.write_text(_YAML % _SQUARE)
    with pytest.raises(ConfigError, match="zones: zone 'approach' is not a valid"):
        load_config(path)
