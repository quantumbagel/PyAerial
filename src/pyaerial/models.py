"""
Core data structures shared across PyAerial and helpers for accessing them.

A :class:`Plane` is the one in-memory aircraft shape: identity and enrichment
(:class:`AircraftInfo`), tracking bookkeeping (:class:`TrackState`), and two
telemetry maps (``received_data``, ``calculated_data``) of field name ->
time-ordered list of :class:`Datum`. Documents (Redis, the archive, the API)
are converted explicitly in :mod:`pyaerial.store.codec`.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from operator import attrgetter
from typing import Any, NamedTuple, NotRequired, TypedDict

from pyaerial.constants import (
    STORE_ALT,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_LAT,
    STORE_LONG,
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


# Telemetry map: field name (STORE_LAT, STORE_HORIZ_SPEED, ...) -> series.
Series = dict[str, list[Datum]]


class Verdict(NamedTuple):
    """One integrity verdict, logged in ``TrackState.integrity_log`` on change."""

    time: float
    # None when the status is unknown (no meaningful score yet).
    score: float | None
    status: str
    # Checks that did not pass (warn or fail), by name.
    flagged: tuple[str, ...]
    # Full check results; None when rebuilt from live telemetry points.
    checks: dict[str, Any] | None = None


class IntegrityDoc(TypedDict):
    """Latest integrity verdict, published in ``AircraftInfo.integrity``."""

    # None while the status is ``unknown``.
    score: float | None
    status: str
    # check name -> CheckResult.as_dict()
    checks: dict[str, dict[str, Any]]
    # Estimated emitter position when the claimed one is contradicted.
    emitter: NotRequired[dict[str, float]]
    rebroadcast: NotRequired[bool]


@dataclass(slots=True)
class AircraftInfo:
    """Identity and enrichment of one aircraft.

    ``None`` means "not known" (absent from the ``info`` document). The info
    document lists fields in the order they were first set, as the dict it
    replaced did: :meth:`doc_order` keeps that order so the Redis and archive
    documents stay byte-identical. ``extra`` holds keys of a restored document
    this version does not model, so they survive archiving.
    """

    # First, so it exists before __init__ assigns (and records) the others.
    _doc_order: list[str] = field(
        default_factory=list, init=False, repr=False, compare=False
    )
    # 24-bit address as decoded (pyModeS hands out upper case).
    icao: str
    callsign: str | None = None
    # [typecode, ADS-B emitter category] from the last identification message.
    plane_category: list[Any] | None = None
    # Receiver of the newest frame, and every receiver that heard the aircraft.
    receiver: str | None = None
    receivers: list[str] | None = None
    integrity: IntegrityDoc | None = None
    # Aircraft database (enrichment).
    registration: str | None = None
    model: str | None = None
    owner: str | None = None
    country: str | None = None
    aircraft_type: str | None = None
    manufacturer: str | None = None
    manufacturer_name: str | None = None
    operator_callsign: str | None = None
    photo_url: str | None = None
    metadata_resolved: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __setattr__(self, name: str, value: object) -> None:
        object.__setattr__(self, name, value)
        if value is not None and name in _INFO_DOC_FIELDS:
            order = self._doc_order
            if name not in order:
                order.append(name)

    def doc_order(self) -> tuple[str, ...]:
        """Document keys (fields and ``extra`` keys) in first-set order."""
        return tuple(self._doc_order)

    def set_extra(self, key: str, value: Any) -> None:
        """Keep an unmodelled document key (in document order)."""
        self.extra[key] = value
        if key not in self._doc_order:
            self._doc_order.append(key)

    def copy(self) -> AircraftInfo:
        """Shallow copy (values shared, like ``dict(info)``), same key order."""
        twin = replace(self, extra=dict(self.extra))
        object.__setattr__(twin, "_doc_order", list(self._doc_order))
        return twin


_INFO_DOC_FIELDS = frozenset(
    f.name for f in fields(AircraftInfo) if f.name not in ("_doc_order", "extra")
)


@dataclass(slots=True)
class TrackState:
    """Tracking bookkeeping for one flight (never published as a document)."""

    # First packet of this flight (moved to the split on a segment split).
    first_packet: float
    # Newest packet.
    last_update: float
    packets: int = 0
    # classify CAT_* -> packets of that category.
    packet_types: dict[int, int] = field(default_factory=dict)
    # Position-fix timestamp -> receivers that heard that frame, strongest first.
    position_receivers: dict[float, tuple[str, ...]] = field(default_factory=dict)
    # Integrity verdict changes, oldest first.
    integrity_log: list[Verdict] = field(default_factory=list)
    # When the engine first tracked the aircraft. Unlike first_packet it is
    # kept when a long track is split into segments (max_flight_seconds).
    track_started: float | None = None
    # Position fixes the CPR jump gate rejected (evidence of two transmitters
    # sharing an ICAO address).
    gate_rejects: int = 0
    # Archive checkpoint cursors: the newest sample timestamp and verdict
    # time already written to this flight's ``live`` archive row, and when
    # that row was last checkpointed (None: the archive has no row yet).
    archived_through: float | None = None
    verdicts_through: float | None = None
    checkpoint_at: float | None = None
    # The segment this flight continues (a split of a long track).
    previous_flight_id: str | None = None


@dataclass(slots=True)
class Plane:
    """One tracked aircraft (one flight)."""

    info: AircraftInfo
    track: TrackState
    received_data: Series = field(default_factory=dict)
    calculated_data: Series = field(default_factory=dict)
    # Alert entries published with the live flight document.
    active_alerts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def icao(self) -> str:
        """ICAO address, lowercased."""
        return self.info.icao.lower()

    @property
    def flight_id(self) -> str:
        """Stable id for one continuous track: ``{icao}-{first_packet_unix}``."""
        return f"{self.info.icao.lower()}-{int(self.track.first_packet)}"


def latest_datum(
    series: Mapping[str, Sequence[Datum]],
    name: str,
    at_time: float | None = None,
) -> Datum | None:
    """
    Return the most relevant :class:`Datum` of ``series[name]``.

    :param series: a telemetry map (``plane.received_data`` / ``calculated_data``)
    :param name: field within it (e.g. ``latitude``)
    :param at_time: if given, return the datum whose timestamp is closest to
        this time; otherwise return the newest datum.
    """
    values = series.get(name)
    if not values:
        return None
    if at_time is None:
        return values[-1]
    return nearest_datum(values, at_time)


# C-level key: bisect over long series calls it O(log n) times per lookup,
# and the engine does millions of lookups (integrity, kinematics, archive).
_datum_time = attrgetter("time")


def nearest_datum(series: Sequence[Datum], at_time: float) -> Datum | None:
    """Return the datum in time-ordered ``series`` closest to ``at_time``.

    Ties go to the later sample. O(log n) via bisection.
    """
    if not series:
        return None
    last = series[-1]
    if at_time >= last.time:
        # Most lookups are for "now" or the newest fix.
        return last
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


def append_datum(series: Series, name: str, datum: Datum) -> bool:
    """
    Append ``datum`` to ``series[name]`` unless it duplicates the latest value
    already stored there.

    :return: whether the datum was actually added
    """
    values = series.get(name)
    if values and values[-1] == datum:
        return False
    if values is None:
        series[name] = [datum]
    else:
        values.append(datum)
    return True


def _verdict_time(verdict: Verdict) -> float:
    return verdict.time


def sample_provenance(plane: Plane, timestamp: float) -> dict[str, Any]:
    """Receivers and integrity verdict for the position sample at *timestamp*.

    ``receivers`` lists the receivers that heard the position frame, the one
    whose copy was kept (strongest) first. The integrity fields are the verdict in effect when the sample
    arrived (the latest one published at or before it), so a sample received
    before the aircraft's first verdict carries none.
    """
    track = plane.track
    out: dict[str, Any] = {}
    heard = track.position_receivers.get(timestamp)
    if heard:
        out["receivers"] = list(heard)
    verdicts = track.integrity_log
    idx = bisect_right(verdicts, timestamp, key=_verdict_time)
    if idx:
        verdict = verdicts[idx - 1]
        out["integrity_score"] = verdict.score
        out["integrity_status"] = verdict.status
        out["integrity_flags"] = list(verdict.flagged)
    return out


def _tail_after(series: Sequence[Datum], since: float | None) -> Sequence[Datum]:
    if since is None:
        return series
    return series[bisect_right(series, since, key=_datum_time) :]


def iter_telemetry_samples(
    plane: Plane,
    since: float | None = None,
) -> Iterator[tuple[float, float, float, float | None, float | None, float | None]]:
    """Yield ``(timestamp, lat, lon, alt, speed, heading)`` for each position sample.

    Walks the union of latitude and longitude timestamps so a constant-latitude
    (east/west) track still emits a point for every longitude sample. When
    ``since`` is given only samples strictly newer than it are produced, and
    only those are visited, so per-tick incremental callers stay O(new · log n).
    """
    recv = plane.received_data
    calc = plane.calculated_data
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
