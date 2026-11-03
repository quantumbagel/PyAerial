"""Airborne position decoding in the tracker: CPR even/odd pairs, local
decode references, range limits and the antimeridian."""

from __future__ import annotations

from helpers import (
    IDENT,
    airborne_position,
    latest_position,
    tracker_at_home,
)
from pyaerial.classify import classify
from pyaerial.config.schema import HomeConfig
from pyaerial.constants import (
    STORE_LAT,
    STORE_LONG,
    STORE_RECV_DATA,
)


def test_aircraft_beyond_180nm_from_antenna_is_not_decoded_into_wrong_zone():
    """Local CPR decoding against the antenna aliases by one zone (~360 NM)
    past 180 NM, and the wrong fix then anchors every later decode."""
    tracker = tracker_at_home()
    true_lat, true_lon = 35.7275 + 3.33, -78.6959  # ~200 NM north
    t = 100.0
    for i in range(6):
        msg = airborne_position("a1b2c3", true_lat + i * 0.001, true_lon, i % 2)
        tracker.ingest([(msg, t)], receivers={msg: ("nc",)})
        t += 0.5
    lat, lon = latest_position(tracker, "A1B2C3")
    assert abs(lat - (true_lat + 0.005)) < 0.01
    assert abs(lon - true_lon) < 0.01
    # No fix was ever placed in the aliased zone.
    lats = [d.value for d in tracker.planes["A1B2C3"][STORE_RECV_DATA][STORE_LAT]]
    assert min(lats) > true_lat - 0.1


def test_first_airborne_fix_waits_for_even_odd_pair():
    tracker = tracker_at_home()
    even = airborne_position("a1b2c3", 36.0, -78.5, 0)
    odd = airborne_position("a1b2c3", 36.001, -78.5, 1)
    tracker.ingest([(even, 1.0)], receivers={even: ("nc",)})
    assert "A1B2C3" not in tracker.planes
    tracker.ingest([(odd, 1.5)], receivers={odd: ("nc",)})
    lat, lon = latest_position(tracker, "A1B2C3")
    assert abs(lat - 36.001) < 0.001
    assert abs(lon - -78.5) < 0.001


def test_pair_further_apart_than_ten_seconds_is_not_used():
    tracker = tracker_at_home()
    even = airborne_position("a1b2c3", 36.0, -78.5, 0)
    odd = airborne_position("a1b2c3", 36.001, -78.5, 1)
    tracker.ingest([(even, 1.0)])
    tracker.ingest([(odd, 20.0)])
    assert "A1B2C3" not in tracker.planes


def test_stale_last_fix_is_not_used_as_local_decode_reference():
    """Only velocity heard for minutes: one position frame decoded against the
    old fix aliases a zone south, and the time-scaled gate accepted it."""
    tracker = tracker_at_home()
    tracker.ingest([(airborne_position("a1b2c3", 36.0, -78.5, 0), 1.0)])
    tracker.ingest([(airborne_position("a1b2c3", 36.001, -78.5, 1), 1.5)])
    tracker.ingest([(airborne_position("a1b2c3", 39.35, -78.5, 0), 451.5)])
    assert abs(latest_position(tracker, "A1B2C3")[0] - 36.001) < 0.001
    tracker.ingest([(airborne_position("a1b2c3", 39.351, -78.5, 1), 452.0)])
    lat, lon = latest_position(tracker, "A1B2C3")
    assert abs(lat - 39.351) < 0.001
    assert abs(lon - -78.5) < 0.001


def test_longitude_is_wrapped_across_the_antimeridian():
    home = HomeConfig(latitude=1.0, longitude=179.95)
    msg = airborne_position("a1b2c3", 1.0, -179.99, 0)
    result = classify(msg, home, last_position=(1.0, 179.95))
    assert result is not None
    lon = result.data[STORE_RECV_DATA][STORE_LONG]
    assert -180.0 <= lon < 180.0
    assert abs(lon - -179.99) < 0.001


def test_tracker_follows_aircraft_across_the_antimeridian():
    tracker = tracker_at_home()
    t = 1.0
    for i, lon in enumerate((179.98, 179.99, -179.995, -179.985)):
        msg = airborne_position("a1b2c3", 1.0, lon, i % 2)
        tracker.ingest([(msg, t)])
        t += 0.5
    lat, lon = latest_position(tracker, "A1B2C3")
    assert abs(lon - -179.985) < 0.001
    lons = [d.value for d in tracker.planes["A1B2C3"][STORE_RECV_DATA][STORE_LONG]]
    assert all(-180.0 <= value < 180.0 for value in lons)


def test_cpr_pair_cache_is_pruned():
    tracker = tracker_at_home()
    for i in range(50):
        icao = f"a1{i:04x}"
        tracker.ingest([(airborne_position(icao, 36.0, -78.5, 0), 1.0)])
    assert len(tracker._cpr_frames) == 50
    tracker.ingest([(IDENT, 100.0)])
    assert tracker._cpr_frames == {}
