"""Per-aircraft rolling evidence gathered from raw frames.

Raw samples are appended as frames arrive. Once a tick, the monitor resolves
new samples against the aircraft's claimed position (which is only known after
the frame batch is decoded) into the ``*_geo`` series, so each sample's
geometry is computed exactly once, and drops the raw ones.

The resolved series are trimmed to ``window_seconds`` by time and decimated
(see :data:`GEO_SPACING`); their sample caps, sized from the window by the
monitor, only bound memory against a flood.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from itertools import islice
from operator import attrgetter, itemgetter
from collections import defaultdict, deque
from dataclasses import dataclass, field

from pyaerial.constants import (
    STORE_ALT,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_LAT,
    STORE_LONG,
)
from pyaerial.integrity import geodesy
from pyaerial.integrity.tdoa import TdoaObservation
from pyaerial.models import Plane, nearest_datum

_SAMPLES = 400
_TIMING_SAMPLES = 240
# Hard cap on timing rows whatever window_seconds is.
TIMING_MAX_SAMPLES = 400
# Raw RSSI / AoA / multi-receiver frames awaiting the next tick.
_RAW_SAMPLES = 64
_TDOA_RAW = 256
# Resolved samples per receiver are kept at most this often (s): frames
# 0.1 s apart share geometry and multipath, so more adds memory, not evidence.
GEO_SPACING = 0.5


def _samples(maxlen: int = _SAMPLES) -> deque:
    return deque(maxlen=maxlen)


@dataclass(slots=True)
class AircraftEvidence:
    last_seen: float
    geo_maxlen: int = _SAMPLES
    resid_maxlen: int = _SAMPLES
    timing_maxlen: int = _TIMING_SAMPLES
    # Heard as TIS-B / ADS-R (DF18, CF >= 2): a ground station retransmits
    # the target, so signal geometry describes the station, not the aircraft.
    rebroadcast: bool = False
    # receiver -> wall time of the last DF17/18 frame it heard
    rx_last: dict[str, float] = field(default_factory=dict)
    # receiver -> (t, rssi_dbfs), until resolved
    rssi: dict[str, deque] = field(
        default_factory=lambda: defaultdict(lambda: _samples(_RAW_SAMPLES))
    )
    # receiver -> (t, rssi_dbfs, slant_km, elevation_deg, 20*log10(slant_km))
    rssi_geo: dict[str, deque] = field(init=False)
    # receiver -> (t, bearing, elevation | None), until resolved
    aoa: dict[str, deque] = field(
        default_factory=lambda: defaultdict(lambda: _samples(_RAW_SAMPLES))
    )
    # receiver -> (t, measured_bearing, expected_bearing, measured_elev, expected_elev)
    aoa_geo: dict[str, deque] = field(init=False)
    # (t, cfo_hz)
    cfo: deque = field(default_factory=_samples)
    # receiver -> (t, clock_ticks, cpr_odd) for airborne position squitters
    pos_clock: dict[str, deque] = field(init=False)
    # (receiver, hex) -> wall time, airborne position frames only (replay check)
    recent_pos: dict[tuple[str, str], float] = field(default_factory=dict)
    replays: deque = field(default_factory=_samples)
    # (t, df) of short Mode S replies carrying this address
    mode_s: deque = field(default_factory=_samples)
    # (t, altitude_m) from DF0/4/16/20 altitude codes
    mode_s_alt: deque = field(default_factory=_samples)
    # Transmissions heard by two or more receivers with clocks; once processed
    # only the newest few are kept (for multilateration).
    tdoa: deque = field(default_factory=lambda: _samples(_TDOA_RAW))
    tdoa_synced_until: float = 0.0
    # (t, path-difference residual m) per synced receiver pair
    tdoa_resid: deque = field(init=False)
    tdoa_cursor: float = 0.0

    def __post_init__(self) -> None:
        geo = self.geo_maxlen
        self.rssi_geo = defaultdict(lambda: _samples(geo))
        self.aoa_geo = defaultdict(lambda: _samples(geo))
        self.tdoa_resid = _samples(self.resid_maxlen)
        timing = self.timing_maxlen
        self.pos_clock = defaultdict(lambda: _samples(timing))

    def add_tdoa(self, obs: TdoaObservation) -> None:
        self.tdoa.append(obs)


def trim(series: deque, cutoff: float) -> None:
    """Drop leading samples (first element is a time) older than *cutoff*."""
    while series and series[0][0] < cutoff:
        series.popleft()


def latest_fix(plane: Plane) -> tuple[float, float, float | None, float] | None:
    """``(lat, lon, alt_m, time)`` of the newest position."""
    recv = plane.received_data
    lat_series = recv.get(STORE_LAT) or []
    lon_series = recv.get(STORE_LONG) or []
    if not lat_series or not lon_series:
        return None
    lat = lat_series[-1]
    lon = lon_series[-1]
    stamp = max(lat.time, lon.time)
    alt = nearest_datum(recv.get(STORE_ALT) or [], stamp)
    return lat.value, lon.value, alt.value if alt is not None else None, stamp


def _series_value(series: list, t: float, slack: float) -> float | None:
    """Value at *t* from a change-compressed series, if *t* is inside it."""
    if not series:
        return None
    if t < series[0].time - slack or t > series[-1].time + slack:
        return None
    datum = nearest_datum(series, t)
    return datum.value if datum is not None else None


def position_at(
    plane: Plane, t: float, max_gap: float = 2.0
) -> tuple[float, float, float | None] | None:
    """Claimed ``(lat, lon, alt_m)`` at wall time *t*, dead-reckoned to *t*."""
    return PositionLookup(plane).at(t, max_gap)


class PositionLookup:
    """:func:`position_at` for many times on one plane: the series are looked
    up once, not per call."""

    __slots__ = ("_lat", "_lon", "_alt", "_plane", "_recv", "_motion")

    def __init__(self, plane: Plane):
        recv = plane.received_data
        self._plane = plane
        self._recv = recv
        self._lat = recv.get(STORE_LAT) or []
        self._lon = recv.get(STORE_LONG) or []
        self._alt = recv.get(STORE_ALT) or []
        self._motion: tuple[list, list] | None = None

    def at(self, t: float, max_gap: float = 2.0) -> tuple[float, float, float | None] | None:
        lat = nearest_datum(self._lat, t)
        lon = nearest_datum(self._lon, t)
        if lat is None or lon is None:
            return None
        fix_time = lat.time if abs(lat.time - t) <= abs(lon.time - t) else lon.time
        gap = t - fix_time
        if abs(gap) > max_gap:
            return None
        alt = _series_value(self._alt, t, max_gap)
        lat_v, lon_v = lat.value, lon.value
        if abs(gap) > 0.05:
            if self._motion is None:
                calc = self._plane.calculated_data
                recv = self._recv
                self._motion = (
                    calc.get(STORE_HORIZ_SPEED) or recv.get(STORE_HORIZ_SPEED) or [],
                    calc.get(STORE_HEADING) or recv.get(STORE_HEADING) or [],
                )
            speed = nearest_datum(self._motion[0], t)
            heading = nearest_datum(self._motion[1], t)
            if speed is not None and heading is not None:
                lat_v, lon_v = geodesy.destination(
                    lat_v, lon_v, heading.value, speed.value / 3.6 * gap
                )
        return lat_v, lon_v, alt


def series_window(series: list, start: float, end: float) -> list:
    """Datums with ``start <= time <= end`` (bisection; series are time-ordered).

    The tracker stores a steady value as one run (first and last datum), so
    a run spanning *start* contributes its value even with no datum inside.
    """
    lo = bisect_left(series, start, key=_DATUM_TIME)
    hi = bisect_right(series, end, key=_DATUM_TIME)
    if 0 < lo < len(series) and series[lo - 1].value == series[lo].value:
        lo -= 1
    return series[lo:hi]


_DATUM_TIME = attrgetter("time")
ROW_TIME = itemgetter(0)


def rows_since(rows: deque, start: float) -> list:
    """Rows (time first, time-ordered) with ``time >= start``."""
    return list(islice(rows, bisect_left(rows, start, key=ROW_TIME), None))
