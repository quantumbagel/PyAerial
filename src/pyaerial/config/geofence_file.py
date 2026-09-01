"""Load zone polygons from KML, KMZ, or GeoJSON files."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


class GeofenceFileError(ValueError):
    """Raised when a geofence file cannot be parsed into a polygon."""


def load_geofence_coordinates(path: Path) -> list[list[float]]:
    """Return ``[[lat, lon], ...]`` from a KML, KMZ, or GeoJSON file."""
    suffix = path.suffix.lower()
    if suffix == ".kmz":
        return _from_kmz(path)
    if suffix == ".kml":
        return _from_kml(path.read_text(encoding="utf-8", errors="replace"))
    if suffix in {".json", ".geojson"}:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GeofenceFileError(f"invalid GeoJSON: {exc}") from exc
        return _from_geojson(payload)
    raise GeofenceFileError(
        f"unsupported geofence file type {suffix or path.name!r}; "
        "use .kml, .kmz, or .geojson"
    )


def _from_kmz(path: Path) -> list[list[float]]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".kml")]
            if not names:
                raise GeofenceFileError("KMZ archive contains no .kml file")
            preferred = next(
                (name for name in names if Path(name).name.lower() == "doc.kml"),
                names[0],
            )
            text = archive.read(preferred).decode("utf-8", errors="replace")
    except zipfile.BadZipFile as exc:
        raise GeofenceFileError(f"invalid KMZ archive: {exc}") from exc
    return _from_kml(text)


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _from_kml(text: str) -> list[list[float]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise GeofenceFileError(f"invalid KML: {exc}") from exc
    for elem in root.iter():
        if _local_tag(elem.tag) != "polygon":
            continue
        points = _kml_polygon_points(elem)
        if points is not None:
            return points
    raise GeofenceFileError("no Polygon coordinates found in KML")


def _kml_polygon_points(polygon: ET.Element) -> list[list[float]] | None:
    outer = None
    for child in polygon:
        if _local_tag(child.tag) in {"outerboundaryis", "outerboundary"}:
            outer = child
            break
    search_root = outer if outer is not None else polygon
    for child in search_root.iter():
        if _local_tag(child.tag) == "coordinates" and child.text:
            points = _kml_coordinate_pairs(child.text)
            if len(points) >= 3:
                return _closed(points)
    return None


def _kml_coordinate_pairs(text: str) -> list[list[float]]:
    points: list[list[float]] = []
    for token in text.strip().split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError:
            continue
        points.append([lat, lon])
    return points


def _from_geojson(obj: object) -> list[list[float]]:
    if not isinstance(obj, dict):
        raise GeofenceFileError("GeoJSON must be an object")
    kind = obj.get("type")
    if kind == "Feature":
        return _from_geojson(obj.get("geometry") or {})
    if kind == "FeatureCollection":
        for feature in obj.get("features") or []:
            try:
                return _from_geojson(feature)
            except GeofenceFileError:
                continue
        raise GeofenceFileError("FeatureCollection has no polygon")
    if kind == "GeometryCollection":
        for geometry in obj.get("geometries") or []:
            try:
                return _from_geojson(geometry)
            except GeofenceFileError:
                continue
        raise GeofenceFileError("GeometryCollection has no polygon")
    if kind == "Polygon":
        return _ring_to_latlon(obj.get("coordinates"))
    if kind == "MultiPolygon":
        polygons = obj.get("coordinates") or []
        if not polygons:
            raise GeofenceFileError("empty MultiPolygon")
        return _ring_to_latlon(polygons[0])
    raise GeofenceFileError(f"unsupported GeoJSON type {kind!r}; need a Polygon")


def _ring_to_latlon(coordinates: object) -> list[list[float]]:
    if not isinstance(coordinates, list) or not coordinates:
        raise GeofenceFileError("empty polygon ring")
    ring = coordinates[0]
    if not isinstance(ring, list):
        raise GeofenceFileError("invalid polygon ring")
    points: list[list[float]] = []
    for point in ring:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            lon = float(point[0])
            lat = float(point[1])
        except (TypeError, ValueError):
            continue
        points.append([lat, lon])
    if len(points) < 3:
        raise GeofenceFileError("polygon needs at least 3 points")
    return _closed(points)


def _closed(points: list[list[float]]) -> list[list[float]]:
    if points[0] != points[-1]:
        return [*points, list(points[0])]
    return points
