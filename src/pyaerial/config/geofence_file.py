"""Load zone polygons from KML, KMZ, or GeoJSON files."""

from __future__ import annotations

import json
import re
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

try:  # Prefer defusedxml when installed; the DTD guard below covers its absence.
    from defusedxml import ElementTree as _SafeET
    from defusedxml.common import DefusedXmlException as _DefusedError
except ImportError:  # pragma: no cover - depends on the environment
    _SafeET = None
    _DefusedError = ValueError

#: Upper bound for a KML/GeoJSON file or the KML member of a KMZ archive.
MAX_GEOFENCE_BYTES = 20 * 1024 * 1024
_DTD_RE = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_POLYGON_TYPES = frozenset({"Polygon", "MultiPolygon", "GeometryCollection"})
_HOLES_UNSUPPORTED = (
    "polygon holes (inner rings) are not supported; the zone would include "
    "the excluded area"
)


class GeofenceFileError(ValueError):
    """Raised when a geofence file cannot be parsed into a polygon."""


def load_geofence_coordinates(path: Path) -> list[list[float]]:
    """Return ``[[lat, lon], ...]`` from a KML, KMZ, or GeoJSON file."""
    suffix = path.suffix.lower()
    if suffix not in {".kmz", ".kml", ".json", ".geojson"}:
        raise GeofenceFileError(
            f"unsupported geofence file type {suffix or path.name!r}; "
            "use .kml, .kmz, or .geojson"
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise GeofenceFileError(f"cannot read geofence file: {exc}") from exc
    if size > MAX_GEOFENCE_BYTES:
        raise GeofenceFileError(
            f"geofence file is too large ({size} bytes; limit {MAX_GEOFENCE_BYTES})"
        )
    if suffix == ".kmz":
        return _from_kmz(path)
    if suffix == ".kml":
        return _from_kml(path.read_text(encoding="utf-8", errors="replace"))
    if suffix in {".json", ".geojson"}:
        try:
            # utf-8-sig: tolerate the BOM some Windows tools write.
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GeofenceFileError(f"invalid GeoJSON: {exc}") from exc
        return _from_geojson(payload)
    raise GeofenceFileError(f"unsupported geofence file type {suffix!r}")


def _from_kmz(path: Path) -> list[list[float]]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = [
                name for name in archive.namelist() if name.lower().endswith(".kml")
            ]
            if not names:
                raise GeofenceFileError("KMZ archive contains no .kml file")
            preferred = next(
                (name for name in names if Path(name).name.lower() == "doc.kml"),
                names[0],
            )
            info = archive.getinfo(preferred)
            if info.file_size > MAX_GEOFENCE_BYTES:
                raise GeofenceFileError(
                    f"KMZ member {preferred!r} is too large "
                    f"({info.file_size} bytes; limit {MAX_GEOFENCE_BYTES})"
                )
            with archive.open(info) as member:
                # Never trust the header size alone: cap the decompressed read.
                raw = member.read(MAX_GEOFENCE_BYTES + 1)
            if len(raw) > MAX_GEOFENCE_BYTES:
                raise GeofenceFileError(
                    f"KMZ member {preferred!r} exceeds {MAX_GEOFENCE_BYTES} bytes"
                )
            text = raw.decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        raise GeofenceFileError(f"invalid KMZ archive: {exc}") from exc
    except (RuntimeError, NotImplementedError) as exc:  # encrypted / unsupported
        raise GeofenceFileError(f"unreadable KMZ archive: {exc}") from exc
    return _from_kml(text)


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse_xml(text: str) -> ET.Element:
    # KML never needs a DTD; refusing one blocks entity expansion (billion
    # laughs) and external entities regardless of the parser in use.
    if _DTD_RE.search(text):
        raise GeofenceFileError("KML must not contain DOCTYPE or ENTITY declarations")
    try:
        if _SafeET is not None:
            return _SafeET.fromstring(text)
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise GeofenceFileError(f"invalid KML: {exc}") from exc
    except _DefusedError as exc:
        raise GeofenceFileError(f"unsafe KML rejected: {exc}") from exc


def _from_kml(text: str) -> list[list[float]]:
    root = _parse_xml(text)
    found: list[list[list[float]]] = []
    for elem in root.iter():
        if _local_tag(elem.tag) != "polygon":
            continue
        points = _kml_polygon_points(elem)
        if points is not None:
            found.append(points)
    if not found:
        raise GeofenceFileError("no Polygon coordinates found in KML")
    if len(found) > 1:
        raise GeofenceFileError(
            "KML contains multiple Polygons; use one polygon per zone file"
        )
    return found[0]


def _kml_polygon_points(polygon: ET.Element) -> list[list[float]] | None:
    outer = None
    for child in polygon:
        tag = _local_tag(child.tag)
        if tag in {"innerboundaryis", "innerboundary"}:
            raise GeofenceFileError(_HOLES_UNSUPPORTED)
        if tag in {"outerboundaryis", "outerboundary"} and outer is None:
            outer = child
    search_root = outer if outer is not None else polygon
    for child in search_root.iter():
        if _local_tag(child.tag) == "coordinates" and child.text:
            return _closed(_kml_coordinate_pairs(child.text))
    return None


def _kml_coordinate_pairs(text: str) -> list[list[float]]:
    points: list[list[float]] = []
    for token in text.strip().split():
        parts = token.split(",")
        if len(parts) < 2:
            raise GeofenceFileError(f"invalid KML coordinate {token[:40]!r}")
        points.append(_lat_lon(parts[0], parts[1], token))
    return points


def _lat_lon(lon_raw: object, lat_raw: object, source: object) -> list[float]:
    """``[lat, lon]`` from file-order ``lon, lat``; never silently drops a vertex."""
    try:
        lon = float(lon_raw)  # type: ignore[arg-type]
        lat = float(lat_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise GeofenceFileError(f"invalid coordinate {str(source)[:40]!r}") from None
    # Dropping an out-of-range vertex (e.g. 0..360 longitudes) would quietly
    # reshape or collapse the zone.
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise GeofenceFileError(
            f"coordinate {str(source)[:40]!r} is outside latitude ±90 / "
            "longitude ±180 (files use longitude, latitude order)"
        )
    return [lat, lon]


def _from_geojson(obj: object) -> list[list[float]]:
    if not isinstance(obj, dict):
        raise GeofenceFileError("GeoJSON must be an object")
    kind = obj.get("type")
    if kind == "Feature":
        return _from_geojson(obj.get("geometry") or {})
    if kind == "FeatureCollection":
        polygons: list[list[list[float]]] = []
        for feature in obj.get("features") or []:
            geometry = feature.get("geometry") if isinstance(feature, dict) else None
            # Skip points/lines, but a broken polygon must fail, not vanish.
            if isinstance(geometry, dict) and geometry.get("type") in _POLYGON_TYPES:
                polygons.append(_from_geojson(geometry))
        if not polygons:
            raise GeofenceFileError("FeatureCollection has no polygon")
        if len(polygons) > 1:
            raise GeofenceFileError(
                "FeatureCollection has multiple polygons; use one polygon per zone file"
            )
        return polygons[0]
    if kind == "GeometryCollection":
        found: list[list[list[float]]] = []
        for geometry in obj.get("geometries") or []:
            if isinstance(geometry, dict) and geometry.get("type") in _POLYGON_TYPES:
                found.append(_from_geojson(geometry))
        if not found:
            raise GeofenceFileError("GeometryCollection has no polygon")
        if len(found) > 1:
            raise GeofenceFileError(
                "GeometryCollection has multiple polygons; use one polygon per zone file"
            )
        return found[0]
    if kind == "Polygon":
        return _ring_to_latlon(obj.get("coordinates"))
    if kind == "MultiPolygon":
        polygons = obj.get("coordinates") or []
        if not polygons:
            raise GeofenceFileError("empty MultiPolygon")
        if len(polygons) > 1:
            raise GeofenceFileError(
                "MultiPolygon has multiple areas; use a single Polygon or split zones"
            )
        return _ring_to_latlon(polygons[0])
    raise GeofenceFileError(f"unsupported GeoJSON type {kind!r}; need a Polygon")


def _ring_to_latlon(coordinates: object) -> list[list[float]]:
    if not isinstance(coordinates, list) or not coordinates:
        raise GeofenceFileError("empty polygon ring")
    if len(coordinates) > 1:
        raise GeofenceFileError(_HOLES_UNSUPPORTED)
    ring = coordinates[0]
    if not isinstance(ring, list):
        raise GeofenceFileError("invalid polygon ring")
    points: list[list[float]] = []
    for point in ring:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise GeofenceFileError(f"invalid coordinate {str(point)[:40]!r}")
        points.append(_lat_lon(point[0], point[1], point))
    return _closed(points)


def _closed(points: list[list[float]]) -> list[list[float]]:
    # Count distinct vertices: [a, b, a] is a line, not a polygon.
    if len({(lat, lon) for lat, lon in points}) < 3:
        raise GeofenceFileError("polygon needs at least 3 distinct points")
    if points[0] != points[-1]:
        return [*points, list(points[0])]
    return points
