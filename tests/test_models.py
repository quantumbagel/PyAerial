from __future__ import annotations

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
    icao_of,
    iter_telemetry_samples,
    last_update,
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
