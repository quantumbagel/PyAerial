"""Per-sample fields shared by the live store and the archive.

A module of its own so the live store (``redis_live``) does not depend on
the SQLite archive (``history``) for them, or the other way round.
"""

from __future__ import annotations

import math
from bisect import bisect_right

from pyaerial.constants import STORE_GNSS_HEIGHT, STORE_RECV_DATA

# Seconds a GNSS height stays attached to later position samples.
GNSS_HEIGHT_MAX_AGE = 10.0


def gnss_height_at(plane: dict, timestamp: float) -> float | None:
    """GNSS height (m, WGS84 ellipsoid) in effect at a position sample, or None.

    TC 20-22 positions carry it instead of barometric altitude. The tracker
    stores a repeated value as the first and last datum of the run, so a
    sample between two equal datums is inside that run. Otherwise the latest
    height at or before the sample is used only while it is fresh, so baro
    positions after an aircraft stops sending GNSS ones get none.
    """
    series = (plane.get(STORE_RECV_DATA) or {}).get(STORE_GNSS_HEIGHT) or []
    idx = bisect_right(series, timestamp, key=lambda datum: datum.time)
    if idx == 0:
        return None
    before = series[idx - 1]
    fresh = timestamp - before.time <= GNSS_HEIGHT_MAX_AGE
    in_run = idx < len(series) and series[idx].value == before.value
    if before.value is None or not (fresh or in_run):
        return None
    value = float(before.value)
    return value if math.isfinite(value) else None
