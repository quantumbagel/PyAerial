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
    type: dump1090
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
          eta: {{ max: 120 }}
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
          eta: {{ max: 120 }}
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
          eta: { max: 120 }
        dwell_seconds: 1
"""
    )
    with pytest.raises(ConfigError, match="not found"):
        load_config(cfg_path)


def test_kml_with_entity_declarations_rejected(tmp_path):
    path = tmp_path / "bomb.kml"
    path.write_text(
        """<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>
<kml><Placemark><Polygon><outerBoundaryIs><LinearRing>
<coordinates>&lol2;</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>
"""
    )
    with pytest.raises(GeofenceFileError, match="DOCTYPE"):
        load_geofence_coordinates(path)


def test_kmz_member_size_is_capped(tmp_path, monkeypatch):
    import pyaerial.config.geofence_file as geofence_file

    kml = _kml(tmp_path)
    kmz = tmp_path / "pad.kmz"
    with zipfile.ZipFile(kmz, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("doc.kml", kml.read_text() + (" " * 50_000))
    assert kmz.stat().st_size < 10_000
    monkeypatch.setattr(geofence_file, "MAX_GEOFENCE_BYTES", 10_000)
    with pytest.raises(GeofenceFileError, match="too large|exceeds"):
        load_geofence_coordinates(kmz)


def _write_geojson(tmp_path: Path, obj: dict, name: str = "zone.geojson") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(obj))
    return path


_SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]


def test_geojson_holes_are_rejected_not_filled(tmp_path):
    hole = [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]
    path = _write_geojson(tmp_path, {"type": "Polygon", "coordinates": [_SQUARE, hole]})
    with pytest.raises(GeofenceFileError, match="holes"):
        load_geofence_coordinates(path)


def test_kml_holes_are_rejected_not_filled(tmp_path):
    path = _kml(tmp_path)
    text = path.read_text().replace(
        "</outerBoundaryIs>",
        "</outerBoundaryIs><innerBoundaryIs><LinearRing><coordinates>"
        "-78.698,35.722 -78.696,35.722 -78.696,35.724 -78.698,35.722"
        "</coordinates></LinearRing></innerBoundaryIs>",
    )
    path.write_text(text)
    with pytest.raises(GeofenceFileError, match="holes"):
        load_geofence_coordinates(path)


def test_out_of_range_vertex_is_an_error_not_silently_dropped(tmp_path):
    # 0..360 longitudes: dropping the >180 vertices used to collapse the
    # square into a zero-area line.
    ring = [[170, 0], [190, 0], [190, 10], [170, 10], [170, 0]]
    path = _write_geojson(tmp_path, {"type": "Polygon", "coordinates": [ring]})
    with pytest.raises(GeofenceFileError, match="outside"):
        load_geofence_coordinates(path)


def test_unparsable_vertex_is_an_error(tmp_path):
    ring = [[0, 0], [1, 0], ["x", 1], [0, 0]]
    path = _write_geojson(tmp_path, {"type": "Polygon", "coordinates": [ring]})
    with pytest.raises(GeofenceFileError, match="invalid coordinate"):
        load_geofence_coordinates(path)
    kml = _kml(tmp_path)
    kml.write_text(kml.read_text().replace("-78.69,35.73,0", "-78.69;35.73"))
    with pytest.raises(GeofenceFileError, match="invalid KML coordinate"):
        load_geofence_coordinates(kml)


def test_degenerate_ring_is_rejected(tmp_path):
    ring = [[0, 0], [1, 1], [0, 0], [1, 1]]
    path = _write_geojson(tmp_path, {"type": "Polygon", "coordinates": [ring]})
    with pytest.raises(GeofenceFileError, match="3 distinct"):
        load_geofence_coordinates(path)


def test_geometry_collection_with_two_polygons_is_rejected(tmp_path):
    first = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    second = {"type": "Polygon", "coordinates": [[[5, 5], [6, 5], [6, 6], [5, 5]]]}
    point = {"type": "Point", "coordinates": [0, 0]}
    path = _write_geojson(
        tmp_path, {"type": "GeometryCollection", "geometries": [first, second]}
    )
    with pytest.raises(GeofenceFileError, match="multiple polygons"):
        load_geofence_coordinates(path)
    path = _write_geojson(
        tmp_path, {"type": "GeometryCollection", "geometries": [point, first]}
    )
    assert load_geofence_coordinates(path)[1] == [0.0, 1.0]


def test_feature_collection_does_not_skip_a_broken_polygon(tmp_path):
    good = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    bad = {"type": "Polygon", "coordinates": [[[0, 0], [200, 0], [1, 1], [0, 0]]]}
    point = {"type": "Point", "coordinates": [0, 0]}
    features = [
        {"type": "Feature", "geometry": geometry} for geometry in (point, bad, good)
    ]
    path = _write_geojson(tmp_path, {"type": "FeatureCollection", "features": features})
    with pytest.raises(GeofenceFileError, match="outside"):
        load_geofence_coordinates(path)


def test_geojson_with_bom_and_bad_encoding(tmp_path):
    path = tmp_path / "bom.geojson"
    body = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(body).encode())
    assert len(load_geofence_coordinates(path)) == 4
    path.write_bytes(b'{"type": "Polygon", "name": "\xe9"}')
    with pytest.raises(GeofenceFileError, match="invalid GeoJSON"):
        load_geofence_coordinates(path)


def test_corrupt_kmz_is_a_geofence_error(tmp_path):
    kmz = tmp_path / "bad.kmz"
    with zipfile.ZipFile(kmz, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("doc.kml", "<kml>" + "x" * 5000 + "</kml>")
    data = bytearray(kmz.read_bytes())
    data[60:70] = b"\xff" * 10  # inside the deflate stream
    kmz.write_bytes(bytes(data))
    with pytest.raises(GeofenceFileError, match="KMZ"):
        load_geofence_coordinates(kmz)


def test_oversized_geofence_file_rejected(tmp_path, monkeypatch):
    import pyaerial.config.geofence_file as geofence_file

    path = _geojson(tmp_path)
    monkeypatch.setattr(geofence_file, "MAX_GEOFENCE_BYTES", 10)
    with pytest.raises(GeofenceFileError, match="too large"):
        load_geofence_coordinates(path)
