from __future__ import annotations

import random
import time

import pytest

from pyaerial.constants import (
    STORE_FIRST_PACKET,
    STORE_ICAO,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_LAT,
    STORE_LONG,
    STORE_MOST_RECENT_PACKET,
    STORE_RECV_DATA,
)
from pyaerial.models import (
    Datum,
    Plane,
    flight_id_for_plane,
    get_latest,
    icao_of,
    iter_telemetry_samples,
    last_update,
    nearest_datum,
)


def _plane() -> dict:
    return {
        STORE_INFO: {STORE_ICAO: "ABC123"},
        STORE_INTERNAL: {
            STORE_FIRST_PACKET: 1_700_000_000.7,
            STORE_MOST_RECENT_PACKET: 1_700_000_010.0,
        },
        STORE_RECV_DATA: {
            STORE_LAT: [Datum(35.72, 1_700_000_000.0), Datum(35.73, 1_700_000_010.0)],
            STORE_LONG: [Datum(-78.70, 1_700_000_000.0), Datum(-78.69, 1_700_000_010.0)],
        },
    }


def test_flight_id_uses_icao_and_first_packet():
    plane = _plane()
    assert icao_of(plane) == "abc123"
    assert flight_id_for_plane(plane) == "abc123-1700000000"
    assert last_update(plane) == 1_700_000_010.0


def test_iter_telemetry_samples_pairs_lat_lon():
    samples = list(iter_telemetry_samples(_plane()))
    assert len(samples) == 2
    ts, lat, lon, alt, speed, heading = samples[0]
    assert ts == 1_700_000_000.0
    assert lat == 35.72
    assert lon == -78.70
    assert alt is None
    assert speed is None
    assert heading is None


def test_iter_telemetry_samples_includes_lon_only_timestamps():
    plane = {
        STORE_INFO: {STORE_ICAO: "abc123"},
        STORE_INTERNAL: {
            STORE_FIRST_PACKET: 1.0,
            STORE_MOST_RECENT_PACKET: 3.0,
        },
        STORE_RECV_DATA: {
            STORE_LAT: [Datum(35.72, 1.0), Datum(35.72, 3.0)],
            STORE_LONG: [Datum(-78.70, 1.0), Datum(-78.69, 2.0), Datum(-78.68, 3.0)],
        },
    }
    samples = list(iter_telemetry_samples(plane))
    assert [item[0] for item in samples] == [1.0, 2.0, 3.0]
    assert samples[1][1] == 35.72
    assert samples[1][2] == -78.69


def test_plane_wraps_mapping_without_copying_buckets():
    raw = _plane()
    plane = Plane.from_mapping(raw)
    assert plane.info is raw[STORE_INFO]
    assert icao_of(plane) == "abc123"
    plane[STORE_INFO]["callsign"] = "N1"
    assert raw[STORE_INFO]["callsign"] == "N1"
    assert plane.setdefault(STORE_RECV_DATA, {}) is raw[STORE_RECV_DATA]


def _long_plane(n: int) -> dict:
    t0 = 1_700_000_000.0
    return {
        STORE_INFO: {STORE_ICAO: "abc123"},
        STORE_INTERNAL: {STORE_FIRST_PACKET: t0, STORE_MOST_RECENT_PACKET: t0 + n},
        STORE_RECV_DATA: {
            STORE_LAT: [Datum(35 + i * 1e-4, t0 + i) for i in range(n)],
            STORE_LONG: [Datum(-78 + i * 1e-4, t0 + i) for i in range(n)],
            "altitude": [Datum(1000 + i, t0 + i + 0.1) for i in range(n)],
        },
        "calculated_data": {
            "speed": [Datum(500, t0 + i) for i in range(n)],
            "heading": [Datum(90, t0 + i) for i in range(n)],
        },
    }


def test_get_latest_nearest_matches_linear_scan():
    rng = random.Random(7)
    times = sorted(rng.choice(range(50)) for _ in range(40))
    series = [Datum(float(i), float(t)) for i, t in enumerate(times)]
    plane = {STORE_RECV_DATA: {STORE_LAT: series}}
    for probe in [x / 2 for x in range(-4, 110)]:
        best = min(abs(d.time - probe) for d in series)
        # Reference: closest time, ties resolved to the later sample.
        expected = [d for d in series if abs(d.time - probe) == best][-1]
        assert nearest_datum(series, probe) is expected
        assert get_latest(STORE_RECV_DATA, STORE_LAT, plane, probe) is expected
    assert nearest_datum([], 1.0) is None


def test_iter_telemetry_samples_since_only_yields_newer():
    plane = _long_plane(10)
    t0 = 1_700_000_000.0
    full = list(iter_telemetry_samples(plane))
    tail = list(iter_telemetry_samples(plane, since=t0 + 6))
    assert [s[0] for s in tail] == [t0 + 7, t0 + 8, t0 + 9]
    assert tail == full[7:]
    assert full[3] == (t0 + 3, 35 + 3e-4, -78 + 3e-4, 1003, 500, 90)


def test_iter_telemetry_samples_is_not_quadratic():
    """Regression: every lookup used to scan backwards, making each tick O(n²)."""
    plane = _long_plane(5000)
    started = time.perf_counter()
    samples = list(iter_telemetry_samples(plane))
    full_elapsed = time.perf_counter() - started
    assert len(samples) == 5000

    started = time.perf_counter()
    for _ in range(100):
        tail = list(iter_telemetry_samples(plane, since=1_700_000_000.0 + 4998))
        assert len(tail) == 1
    tick_elapsed = time.perf_counter() - started
    # Generous bounds: the O(n²) version took seconds for this.
    assert full_elapsed < 0.5
    assert tick_elapsed < 0.1


def test_datum_equality_is_by_value_and_unhashable():
    assert Datum(1.0, 2.0) == Datum(1.0, 2.0)
    assert Datum(1.0, 2.0) != Datum(1.0, 3.0)
    assert Datum(1.0, 2.0) != (1.0, 2.0)
    with pytest.raises(TypeError):
        hash(Datum(1.0, 2.0))
