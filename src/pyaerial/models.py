"""
Core data structures shared across PyAerial and helpers for accessing them.

A "plane" is a nested dict with four buckets (``info``, ``received_data``,
``calculated_data``, ``internal``). Telemetry/calculated values are stored as
lists of :class:`Datum` (value/timestamp pairs) so history can be filtered and
saved later.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, TypedDict


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
    raw_messages: list[Any]


def get_latest(information_type: str, information_datum: str, plane_data: PlaneState | dict,
               after_time: float | None = None) -> Datum | None:
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


def patch_append(plane: PlaneState | dict, bucket: str, field: str, datum: Datum) -> bool:
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
