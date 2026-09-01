"""
Core data structures shared across PyAerial and helpers for accessing them.

A :class:`Plane` holds four buckets (``info``, ``received_data``,
``calculated_data``, ``internal``). Telemetry/calculated values are stored as
lists of :class:`Datum` (value/timestamp pairs). Dict-style bucket access is
supported so existing call sites keep working.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
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


@dataclass(slots=True)
class Datum:
    """A single value observed (or calculated) at a point in time."""

    value: float
    time: float

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Datum):
            return False
        return self.value == other.value and self.time == other.time

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

    best = None
    best_delta = math.inf
    for item in reversed(series):
        delta = abs(item.time - after_time)
        if delta < best_delta:
            best = item
            best_delta = delta
        else:
            # Series is ordered, so once we start getting further away we stop.
            break
    return best


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


def iter_telemetry_samples(
    plane: Plane | PlaneState | dict,
) -> Iterator[tuple[float, float, float, float | None, float | None, float | None]]:
    """Yield ``(timestamp, lat, lon, alt, speed, heading)`` for each position sample."""
    lat_series = plane.get(STORE_RECV_DATA, {}).get(STORE_LAT, [])
    for lat_datum in lat_series:
        lon_datum = get_latest(STORE_RECV_DATA, STORE_LONG, plane, lat_datum.time)
        if lon_datum is None:
            continue
        alt_datum = get_latest(STORE_RECV_DATA, STORE_ALT, plane, lat_datum.time)
        speed_datum = get_latest(
            STORE_CALC_DATA, STORE_HORIZ_SPEED, plane, lat_datum.time
        ) or get_latest(STORE_RECV_DATA, STORE_HORIZ_SPEED, plane, lat_datum.time)
        heading_datum = get_latest(
            STORE_CALC_DATA, STORE_HEADING, plane, lat_datum.time
        ) or get_latest(STORE_RECV_DATA, STORE_HEADING, plane, lat_datum.time)
        yield (
            lat_datum.time,
            lat_datum.value,
            lon_datum.value,
            alt_datum.value if alt_datum is not None else None,
            speed_datum.value if speed_datum is not None else None,
            heading_datum.value if heading_datum is not None else None,
        )
