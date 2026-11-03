from __future__ import annotations

import random
import time

import pytest

from pyaerial.constants import (
    STORE_LAT,
    STORE_LONG,
)
from pyaerial.models import (
    AircraftInfo,
    Datum,
    Plane,
    TrackState,
    Verdict,
    iter_telemetry_samples,
    latest_datum,
    nearest_datum,
    sample_provenance,
)


def _plane() -> Plane:
    return Plane(
        info=AircraftInfo(icao="ABC123"),
        track=TrackState(first_packet=1_700_000_000.7, last_update=1_700_000_010.0),
        received_data={
            STORE_LAT: [Datum(35.72, 1_700_000_000.0), Datum(35.73, 1_700_000_010.0)],
            STORE_LONG: [
                Datum(-78.70, 1_700_000_000.0),
                Datum(-78.69, 1_700_000_010.0),
            ],
        },
    )


def test_flight_id_uses_icao_and_first_packet():
    plane = _plane()
    assert plane.icao == "abc123"
    assert plane.flight_id == "abc123-1700000000"
    assert plane.track.last_update == 1_700_000_010.0


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
    plane = Plane(
        info=AircraftInfo(icao="abc123"),
        track=TrackState(first_packet=1.0, last_update=3.0),
        received_data={
            STORE_LAT: [Datum(35.72, 1.0), Datum(35.72, 3.0)],
            STORE_LONG: [Datum(-78.70, 1.0), Datum(-78.69, 2.0), Datum(-78.68, 3.0)],
        },
    )
    samples = list(iter_telemetry_samples(plane))
    assert [item[0] for item in samples] == [1.0, 2.0, 3.0]
    assert samples[1][1] == 35.72
    assert samples[1][2] == -78.69


def test_info_doc_lists_fields_in_first_set_order():
    info = AircraftInfo(icao="ABC123")
    info.receiver = "roof"
    info.receivers = []
    info.receivers.append("roof")
    info.callsign = "N1"
    info.receiver = "mast"  # already set: keeps its place
    info.integrity = {"score": None, "status": "unknown", "checks": {}}
    info.registration = None  # unset stays absent
    assert info.doc_order() == (
        "icao",
        "receiver",
        "receivers",
        "callsign",
        "integrity",
    )
    twin = info.copy()
    twin.model = "C172"
    assert twin.doc_order() == (*info.doc_order(), "model")
    assert info.doc_order()[-1] == "integrity"
    assert twin.receivers is info.receivers  # shallow, like dict(info)


def test_info_doc_round_trip_keeps_order_and_unknown_keys():
    from pyaerial.store.codec import info_doc, info_from_doc

    raw = {
        "icao": "ABC123",
        "receiver": "roof",
        "operator": "Example Air",  # not modelled
        "plane_category": [4, 3],
        "callsign": "",
        "model": None,
        "metadata_resolved": True,
    }
    info = info_from_doc(raw, "abc123")
    assert info.icao == "abc123"
    assert info.callsign == ""
    assert info.model is None
    assert info.extra == {"operator": "Example Air", "model": None}
    assert info_doc(info) == {**raw, "icao": "abc123"}
    assert list(info_doc(info)) == list(raw)


def _long_plane(n: int) -> Plane:
    t0 = 1_700_000_000.0
    return Plane(
        info=AircraftInfo(icao="abc123"),
        track=TrackState(first_packet=t0, last_update=t0 + n),
        received_data={
            STORE_LAT: [Datum(35 + i * 1e-4, t0 + i) for i in range(n)],
            STORE_LONG: [Datum(-78 + i * 1e-4, t0 + i) for i in range(n)],
            "altitude": [Datum(1000 + i, t0 + i + 0.1) for i in range(n)],
        },
        calculated_data={
            "speed": [Datum(500, t0 + i) for i in range(n)],
            "heading": [Datum(90, t0 + i) for i in range(n)],
        },
    )


def test_latest_datum_nearest_matches_linear_scan():
    rng = random.Random(7)
    times = sorted(rng.choice(range(50)) for _ in range(40))
    series = [Datum(float(i), float(t)) for i, t in enumerate(times)]
    recv = {STORE_LAT: series}
    for probe in [x / 2 for x in range(-4, 110)]:
        best = min(abs(d.time - probe) for d in series)
        # Reference: closest time, ties resolved to the later sample.
        expected = [d for d in series if abs(d.time - probe) == best][-1]
        assert nearest_datum(series, probe) is expected
        assert latest_datum(recv, STORE_LAT, probe) is expected
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


def test_sample_provenance_uses_verdict_in_effect_when_sample_arrived():
    plane = _plane()
    plane.track.position_receivers = {2.0: ("nl", "be")}
    plane.track.integrity_log = [
        Verdict(1.5, 0.9, "ok", ()),
        Verdict(3.0, 0.3, "spoofed", ("aoa",)),
    ]
    assert sample_provenance(plane, 1.0) == {}
    assert sample_provenance(plane, 2.0) == {
        "receivers": ["nl", "be"],
        "integrity_score": 0.9,
        "integrity_status": "ok",
        "integrity_flags": [],
    }
    assert sample_provenance(plane, 3.0)["integrity_flags"] == ["aoa"]


# --- nearest_datum fast path -------------------------------------------------


def test_nearest_datum_fast_path_for_latest():
    series = [Datum(1.0, 10.0), Datum(2.0, 20.0), Datum(3.0, 20.0)]
    assert nearest_datum(series, 20.0) is series[-1]
    assert nearest_datum(series, 99.0) is series[-1]
    assert nearest_datum(series, 16.0) is series[-1]  # tie on equal times -> last
    assert nearest_datum(series, 14.0) is series[0]
