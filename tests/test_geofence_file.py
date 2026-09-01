from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from pyaerial.config.geofence_file import GeofenceFileError, load_geofence_coordinates
from pyaerial.config.loader import ConfigError, load_config


_RING = [
    [35.72, -78.70],
    [35.73, -78.70],
    [35.73, -78.69],
    [35.72, -78.70],
]

_MINIMAL_HOME = """
home:
  latitude: 35.7275
  longitude: -78.6959
receivers:
  main:
    type: mock
"""


def _geojson(tmp_path: Path) -> Path:
    path = tmp_path / "pad.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "Polygon",
                "coordinates": [[[lon, lat] for lat, lon in _RING]],
            }
        )
    )
    return path


def _kml(tmp_path: Path) -> Path:
    coords = " ".join(f"{lon},{lat},0" for lat, lon in _RING)
    path = tmp_path / "pad.kml"
    path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <Polygon>
        <outerBoundaryIs>
          <LinearRing>
            <coordinates>{coords}</coordinates>
          </LinearRing>
        </outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>
"""
    )
    return path


def test_load_geojson_polygon(tmp_path):
    points = load_geofence_coordinates(_geojson(tmp_path))
    assert points[0] == [35.72, -78.70]
    assert points[-1] == points[0]
    assert len(points) >= 4


def test_load_kml_polygon(tmp_path):
    points = load_geofence_coordinates(_kml(tmp_path))
    assert [35.72, -78.70] in points
    assert [35.73, -78.69] in points


def test_load_kmz_polygon(tmp_path):
    kml = _kml(tmp_path)
    kmz = tmp_path / "pad.kmz"
    with zipfile.ZipFile(kmz, "w") as archive:
        archive.write(kml, arcname="doc.kml")
    points = load_geofence_coordinates(kmz)
    assert len(points) >= 4


def test_unsupported_extension(tmp_path):
    path = tmp_path / "pad.txt"
    path.write_text("nope")
    with pytest.raises(GeofenceFileError, match="unsupported"):
        load_geofence_coordinates(path)


def test_load_config_from_geojson_file(tmp_path):
    geojson = _geojson(tmp_path)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        _MINIMAL_HOME
        + f"""
zones:
  pad:
    file: {geojson.name}
    rules:
      - name: warn
        when:
          altitude: {{ max: 2000 }}
        dwell_seconds: 1
"""
    )
    config = load_config(cfg_path)
    assert config.zones["pad"].coordinates is not None
    assert len(config.zones["pad"].coordinates) >= 3
    assert config.zones["pad"].coordinates[0] == [35.72, -78.70]


def test_file_and_coordinates_rejected(tmp_path):
    geojson = _geojson(tmp_path)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        _MINIMAL_HOME
        + f"""
zones:
  pad:
    file: {geojson.name}
    coordinates: [[35.72, -78.70], [35.73, -78.70], [35.73, -78.69]]
    rules:
      - name: warn
        when:
          altitude: {{ max: 2000 }}
        dwell_seconds: 1
"""
    )
    with pytest.raises(ConfigError, match="coordinates or file"):
        load_config(cfg_path)


def test_missing_geofence_file(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        _MINIMAL_HOME
        + """
zones:
  pad:
    file: missing.geojson
    rules:
      - name: warn
        when:
          altitude: { max: 2000 }
        dwell_seconds: 1
"""
    )
    with pytest.raises(ConfigError, match="not found"):
        load_config(cfg_path)
