"""
Core data structures shared across PyAerial and helpers for accessing them.

A :class:`Plane` holds four buckets (``info``, ``received_data``,
``calculated_data``, ``internal``). Telemetry/calculated values are stored as
lists of :class:`Datum` (value/timestamp pairs). Dict-style bucket access is
supported so existing call sites keep working.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, TypedDict

from pyaerial.constants import (
    STORE_ALT,
    STORE_CALC_DATA,
    STORE_FIRST_PACKET,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_MOST_RECENT_PACKET,
    STORE_RECV_DATA,
)


@dataclass(slots=True, eq=True)
class Datum:
    """A single value observed (or calculated) at a point in time.

    Equality is the dataclass-generated field comparison. Datum is mutable, so
    (like any ``eq=True`` dataclass) it is deliberately unhashable.
    """

    value: float
    time: float

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return f"Datum({self.value}, {self.time})"


class PlaneState(TypedDict, total=False):
    """In-memory plane document keyed by telemetry buckets."""

    info: dict[str, Any]
    received_data: dict[str, list[Datum]]
    calculated_data: dict[str, list[Datum]]
    internal: dict[str, Any]
    active_alerts: list[dict[str, Any]]


_PLANE_ATTR = {
    STORE_INFO: "info",
    STORE_RECV_DATA: "received_data",
    STORE_CALC_DATA: "calculated_data",
    STORE_INTERNAL: "internal",
    "active_alerts": "active_alerts",
}


@dataclass(slots=True)
class Plane:
    """Typed in-memory plane. Dict-style bucket access still works."""

    info: dict[str, Any] = field(default_factory=dict)
    received_data: dict[str, list[Datum]] = field(default_factory=dict)
    calculated_data: dict[str, list[Datum]] = field(default_factory=dict)
    internal: dict[str, Any] = field(default_factory=dict)
    active_alerts: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, data: "Plane | dict") -> "Plane":
        """Wrap ``data``, sharing the inner bucket dicts (no copy)."""
        if isinstance(data, Plane):
            return data
        return cls(
            info=data.setdefault(STORE_INFO, {}),
            received_data=data.setdefault(STORE_RECV_DATA, {}),
            calculated_data=data.setdefault(STORE_CALC_DATA, {}),
            internal=data.setdefault(STORE_INTERNAL, {}),
            active_alerts=data.setdefault("active_alerts", [])
            if "active_alerts" in data
            else list(data.get("active_alerts") or []),
        )

    def __getitem__(self, key: str) -> Any:
        attr = _PLANE_ATTR.get(key)
        if attr is None:
            raise KeyError(key)
        return getattr(self, attr)

    def __setitem__(self, key: str, value: Any) -> None:
        attr = _PLANE_ATTR.get(key)
        if attr is None:
            raise KeyError(key)
        setattr(self, attr, value)

    def __contains__(self, key: object) -> bool:
        return key in _PLANE_ATTR

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def setdefault(self, key: str, default: Any) -> Any:
        try:
            return self[key]
        except KeyError:
            self[key] = default
            return default


def get_latest(
    information_type: str,
    information_datum: str,
    plane_data: Plane | PlaneState | dict,
    after_time: float | None = None,
) -> Datum | None:
    """
    Return the most relevant :class:`Datum` for a field.

    :param information_type: bucket (e.g. ``received_data``)
    :param information_datum: field within the bucket (e.g. ``latitude``)
    :param plane_data: the plane dict to read from
    :param after_time: if given, return the datum whose timestamp is closest to
        this time; otherwise return the newest datum.
    """
    bucket = plane_data.get(information_type)
    if not bucket:
        return None
    series = bucket.get(information_datum)
    if not series:
        return None

    if after_time is None:
        return series[-1]
    return nearest_datum(series, after_time)


def _datum_time(datum: Datum) -> float:
    return datum.time


def nearest_datum(series: Sequence[Datum], at_time: float) -> Datum | None:
    """Return the datum in time-ordered ``series`` closest to ``at_time``.

    Ties go to the later sample. O(log n) via bisection.
    """
    if not series:
        return None
    idx = bisect_right(series, at_time, key=_datum_time)
    if idx == len(series):
        return series[-1]
    after = series[idx]
    # Among equal-time samples after ``at_time`` prefer the last one.
    if idx + 1 < len(series) and series[idx + 1].time == after.time:
        after = series[bisect_right(series, after.time, key=_datum_time) - 1]
    if idx == 0:
        return after
    before = series[idx - 1]
    if after.time - at_time <= at_time - before.time:
        return after
    return before


def patch_append(
    plane: Plane | PlaneState | dict, bucket: str, field: str, datum: Datum
) -> bool:
    """
    Append ``datum`` to ``plane[bucket][field]`` unless it duplicates the latest
    value already stored there.

    :return: whether the datum was actually added
    """
    latest = get_latest(bucket, field, plane)
    if latest == datum:
        return False
    plane.setdefault(bucket, {}).setdefault(field, []).append(datum)
    return True


def icao_of(plane: Plane | PlaneState | dict) -> str:
    """Return the plane's ICAO address, lowercased."""
    info = plane.info if isinstance(plane, Plane) else plane[STORE_INFO]
    return info[STORE_ICAO].lower()


def first_packet_time(plane: Plane | PlaneState | dict) -> float:
    internal = plane.internal if isinstance(plane, Plane) else plane[STORE_INTERNAL]
    return internal[STORE_FIRST_PACKET]


def last_update(plane: Plane | PlaneState | dict) -> float:
    internal = plane.internal if isinstance(plane, Plane) else plane[STORE_INTERNAL]
    return internal[STORE_MOST_RECENT_PACKET]


def flight_id_for_plane(plane: Plane | PlaneState | dict) -> str:
    """Stable id for one continuous track: ``{icao}-{first_packet_unix}``."""
    return f"{icao_of(plane)}-{int(first_packet_time(plane))}"


def _tail_after(series: Sequence[Datum], since: float | None) -> Sequence[Datum]:
    if since is None:
        return series
    return series[bisect_right(series, since, key=_datum_time):]


def iter_telemetry_samples(
    plane: Plane | PlaneState | dict,
    since: float | None = None,
) -> Iterator[tuple[float, float, float, float | None, float | None, float | None]]:
    """Yield ``(timestamp, lat, lon, alt, speed, heading)`` for each position sample.

    Walks the union of latitude and longitude timestamps so a constant-latitude
    (east/west) track still emits a point for every longitude sample. When
    ``since`` is given only samples strictly newer than it are produced, and
    only those are visited, so per-tick incremental callers stay O(new · log n).
    """
    recv = plane.get(STORE_RECV_DATA, {}) or {}
    calc = plane.get(STORE_CALC_DATA, {}) or {}
    lat_series = recv.get(STORE_LAT) or []
    lon_series = recv.get(STORE_LONG) or []
    if not lat_series or not lon_series:
        return
    alt_series = recv.get(STORE_ALT) or []
    speed_series = calc.get(STORE_HORIZ_SPEED) or recv.get(STORE_HORIZ_SPEED) or []
    heading_series = calc.get(STORE_HEADING) or recv.get(STORE_HEADING) or []
    times = sorted(
        {datum.time for datum in _tail_after(lat_series, since)}
        | {datum.time for datum in _tail_after(lon_series, since)}
    )
    for stamp in times:
        lat_datum = nearest_datum(lat_series, stamp)
        lon_datum = nearest_datum(lon_series, stamp)
        if lat_datum is None or lon_datum is None:
            continue
        alt_datum = nearest_datum(alt_series, stamp)
        speed_datum = nearest_datum(speed_series, stamp)
        heading_datum = nearest_datum(heading_series, stamp)
        yield (
            stamp,
            lat_datum.value,
            lon_datum.value,
            alt_datum.value if alt_datum is not None else None,
            speed_datum.value if speed_datum is not None else None,
            heading_datum.value if heading_datum is not None else None,
        )
