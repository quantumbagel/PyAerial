"""Per-aircraft rolling evidence gathered from raw frames.

Raw samples are appended as frames arrive. Once a tick, the monitor resolves
new samples against the aircraft's claimed position (which is only known after
the frame batch is decoded) into the ``*_geo`` series, so each sample's
geometry is computed exactly once.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from dataclasses import dataclass, field

from pyaerial.constants import (
    STORE_ALT,
    STORE_CALC_DATA,
    STORE_HEADING,
    STORE_HORIZ_SPEED,
    STORE_LAT,
    STORE_LONG,
    STORE_RECV_DATA,
)
from pyaerial.integrity import geodesy
from pyaerial.integrity.tdoa import TdoaObservation
from pyaerial.models import nearest_datum

_SAMPLES = 400
_TIMING_SAMPLES = 240


def _samples(maxlen: int = _SAMPLES) -> deque:
    return deque(maxlen=maxlen)


@dataclass(slots=True)
class AircraftEvidence:
    last_seen: float
    # Heard as TIS-B / ADS-R (DF18, CF >= 2): a ground station retransmits
    # the target, so signal geometry describes the station, not the aircraft.
    rebroadcast: bool = False
    # receiver -> wall time of the last DF17/18 frame it heard
    rx_last: dict[str, float] = field(default_factory=dict)
    # receiver -> (t, rssi_dbfs)
    rssi: dict[str, deque] = field(default_factory=lambda: defaultdict(_samples))
    # receiver -> (t, rssi_dbfs, slant_km, elevation_deg)
    rssi_geo: dict[str, deque] = field(default_factory=lambda: defaultdict(_samples))
    # receiver -> (t, bearing, elevation | None)
    aoa: dict[str, deque] = field(default_factory=lambda: defaultdict(_samples))
    # receiver -> (t, measured_bearing, expected_bearing, measured_elev, expected_elev)
    aoa_geo: dict[str, deque] = field(default_factory=lambda: defaultdict(_samples))
    # (t, cfo_hz)
    cfo: deque = field(default_factory=_samples)
    # receiver -> (clock_ticks, cpr_odd) for airborne position squitters
    pos_clock: dict[str, deque] = field(
        default_factory=lambda: defaultdict(lambda: _samples(_TIMING_SAMPLES))
    )
    # (receiver, hex) -> wall time, airborne position frames only (replay check)
    recent_pos: dict[tuple[str, str], float] = field(default_factory=dict)
    replays: deque = field(default_factory=_samples)
    # (t, df) of short Mode S replies carrying this address
    mode_s: deque = field(default_factory=_samples)
    # (t, altitude_m) from DF0/4/16/20 altitude codes
    mode_s_alt: deque = field(default_factory=_samples)
    # Transmissions heard by two or more receivers with clocks
    tdoa: deque = field(default_factory=_samples)
    tdoa_synced_until: float = 0.0
    # (t, path-difference residual m) per synced receiver pair
    tdoa_resid: deque = field(default_factory=_samples)
    tdoa_cursor: float = 0.0
    # Cursors: newest raw sample already resolved into *_geo
    rssi_cursor: dict[str, float] = field(default_factory=dict)
    aoa_cursor: dict[str, float] = field(default_factory=dict)

    def add_tdoa(self, obs: TdoaObservation) -> None:
        self.tdoa.append(obs)


def trim(series: deque, cutoff: float) -> None:
    """Drop leading samples (first element is a time) older than *cutoff*."""
    while series and series[0][0] < cutoff:
        series.popleft()


def latest_fix(plane: dict) -> tuple[float, float, float | None, float] | None:
    """``(lat, lon, alt_m, time)`` of the newest position."""
    recv = plane.get(STORE_RECV_DATA) or {}
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
    plane: dict, t: float, max_gap: float = 2.0
) -> tuple[float, float, float | None] | None:
    """Claimed ``(lat, lon, alt_m)`` at wall time *t*, dead-reckoned to *t*."""
    recv = plane.get(STORE_RECV_DATA) or {}
    lat_series = recv.get(STORE_LAT) or []
    lon_series = recv.get(STORE_LONG) or []
    lat = nearest_datum(lat_series, t)
    lon = nearest_datum(lon_series, t)
    if lat is None or lon is None:
        return None
    fix_time = lat.time if abs(lat.time - t) <= abs(lon.time - t) else lon.time
    gap = t - fix_time
    if abs(gap) > max_gap:
        return None
    alt = _series_value(recv.get(STORE_ALT) or [], t, max_gap)
    lat_v, lon_v = lat.value, lon.value
    if abs(gap) > 0.05:
        calc = plane.get(STORE_CALC_DATA) or {}
        speed = nearest_datum(calc.get(STORE_HORIZ_SPEED) or recv.get(STORE_HORIZ_SPEED) or [], t)
        heading = nearest_datum(calc.get(STORE_HEADING) or recv.get(STORE_HEADING) or [], t)
        if speed is not None and heading is not None:
            lat_v, lon_v = geodesy.destination(
                lat_v, lon_v, heading.value, speed.value / 3.6 * gap
            )
    return lat_v, lon_v, alt


def series_window(series: list, start: float, end: float) -> list:
    """Datums with ``start <= time <= end`` (bisection; series are time-ordered)."""
    lo = bisect_left(series, start, key=_datum_time)
    hi = bisect_right(series, end, key=_datum_time)
    return series[lo:hi]


def _datum_time(datum) -> float:
    return datum.time
