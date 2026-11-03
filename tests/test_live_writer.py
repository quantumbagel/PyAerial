"""LiveWriter, the engine's live store: a cache of what it tracks. The tick's
publish (flights, telemetry, episodes, deletions), the writer claim, and
republishing after Redis comes back."""

from __future__ import annotations

import json
import time

import pytest

from fake_redis import FakeRedis
from helpers import (
    LOCAL_REDIS_URI,
    alert_meta,
    fine_track_plane,
    gnss_then_baro_plane,
    heading_plane,
    track_plane,
    with_unknown_then_ok,
)
from pyaerial.api import queries
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
)
from pyaerial.store.codec import alert_episode_doc
from pyaerial.store.live import LiveUnavailable, keys
from pyaerial.store.live.writer import LiveWriter

URI = "redis://user:s3cret@redis.example:6380/0"


def _episode(plane, alert_id, *, active=True, at=5.0, last=None):
    doc = alert_episode_doc(
        plane,
        alert_meta(plane.icao),
        {},
        alert_id=alert_id,
        activated_at=at,
        active=active,
        deactivated_at=None if active else (last or at),
    )
    if last is not None:
        doc["last_updated"] = last
    return doc


def _plane(icao: str = "abc123", t: float | None = None) -> Plane:
    t = time.time() if t is None else t
    return Plane(
        info=AircraftInfo(icao=icao),
        track=TrackState(first_packet=t, last_update=t),
        received_data={
            STORE_LAT: [Datum(35.0, t)],
            STORE_LONG: [Datum(-78.0, t)],
        },
    )


def test_get_flights_batches_round_trips_and_count_uses_scard(fake):
    store = LiveWriter(URI)
    store.write_live_planes({f"p{n}": _plane(f"a{n:05x}") for n in range(20)})
    fake.calls.clear()
    flights = store.get_flights()
    assert len(flights) == 20
    assert all(f.get("latitude") == 35.0 for f in flights)
    # smembers + one pipelined batch, not 2 calls per flight.
    assert fake.calls.count("get") == 0
    assert fake.calls.count("zrevrange") == 0
    assert fake.calls.count("execute") == 1
    fake.calls.clear()
    assert store.count_flights() == 20
    assert fake.calls == ["scard"]


def test_failed_upsert_marks_disconnected(fake):
    store = LiveWriter(URI)
    fake.fail.add("execute")
    store.write_live_planes({"p": _plane()})
    assert store.client is None


def test_failed_deactivation_does_not_leave_ghost_active_alert(fake):
    store = LiveWriter(URI)
    assert store.claim_engine()
    plane = _plane()
    store.cache_episode(_episode(plane, "a1"))
    store.write_live_planes({"p": plane})
    assert "a1" in fake.hashes["live:active_alerts"]

    fake.fail.add("execute")
    store.cache_episode(_episode(plane, "a1", active=False, last=2.0))
    store.write_live_planes({"p": plane})
    assert store.client is None  # write failure was noticed
    assert "a1" in fake.hashes["live:active_alerts"]  # Redis still has the ghost

    fake.fail.clear()
    store._last_connect_attempt = 0.0
    assert store.ping()
    store.write_live_planes({"p": plane})  # the next tick republishes
    assert "a1" not in fake.hashes["live:active_alerts"]


def test_stale_disconnect_does_not_drop_fresh_client(fake):
    store = LiveWriter(URI)
    old = store.client
    replacement = FakeRedis()
    store.client = replacement
    store._mark_disconnected(RuntimeError("late"), old)
    assert store.client is replacement


def test_unchanged_flights_are_not_rewritten(fake):
    store = LiveWriter(URI)
    planes = {f"p{n}": _plane(f"a{n:05x}", t=1000.0) for n in range(5)}
    store.write_live_planes(planes)
    assert fake.calls.count("execute") == 1  # one round trip for all five
    fake.calls.clear()
    store.write_live_planes(planes)
    assert fake.calls.count("execute") == 0
    planes["p0"].info.callsign = "NEW1"
    store.write_live_planes(planes)
    assert fake.calls.count("execute") == 1
    assert '"NEW1"' in fake.kv["live:flight:a00000-1000"]


# --- Redis writer ownership -------------------------------------------------


def test_ensure_writer_survives_redis_error(fake):
    """A Redis outage hitting the per-tick claim must not look like a rival engine."""
    store = LiveWriter(LOCAL_REDIS_URI)
    assert store.claim_engine()
    fake.down = True  # Redis restarts; the last ping was < 2 s ago
    assert store.ensure_writer() is True
    assert store.writer is True
    assert store.client is None  # the failure was noticed

    fake.down = False
    store._last_connect_attempt = 0.0
    assert store.ensure_writer() is True
    assert store.writer is True
    assert store._owns_heartbeat(json.loads(fake.kv["live:engine"]))


def test_reconnect_claim_error_retries_instead_of_yielding(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    assert store.claim_engine()
    store._mark_disconnected(RuntimeError("drop"), store.client)
    fake.fail.add("set")  # PING works, but the claim write fails
    store._last_connect_attempt = 0.0
    store.ping()
    assert store.writer is True
    assert store.client is None  # retried on the next reconnect

    fake.fail.clear()
    store._last_connect_attempt = 0.0
    assert store.ping() is True
    assert store.writer is True
    assert store._owns_heartbeat(json.loads(fake.kv["live:engine"]))


def test_reconnect_still_yields_to_a_fresh_rival(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    assert store.claim_engine()
    store._mark_disconnected(RuntimeError("drop"), store.client)
    fake.kv.pop("live:engine")
    rival = LiveWriter(LOCAL_REDIS_URI)
    assert rival.claim_engine()
    store._last_connect_attempt = 0.0
    store.ping()
    assert store.writer is False


# --- Safety-net TTL on per-flight live keys ----------------------------------


def test_live_flight_keys_carry_a_refreshed_safety_ttl(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    assert store.claim_engine()
    plane = heading_plane("abc123", time.time())
    fid = f"abc123-{int(plane.track.first_packet)}"
    store.cache_episode(_episode(plane, "a1", at=1.0))
    store.write_live_planes({"ABC123": plane})
    ttl = keys.LIVE_FLIGHT_TTL_SECONDS
    # Must outlive any engine downtime we still want to archive after.
    assert ttl >= 3 * 86_400
    for key in (f"live:flight:{fid}", f"live:telemetry:{fid}", f"live:alerts:{fid}"):
        assert fake.ttls[key] == ttl, key
    # Shared indexes never expire as a whole.
    assert "live:flights" not in fake.ttls
    assert "live:active_alerts" not in fake.ttls

    # Refreshed on write: the next sample re-arms telemetry, the doc re-arms itself.
    fake.ttls.clear()
    plane.received_data[STORE_LAT].append(Datum(35.1, time.time() + 5))
    plane.received_data[STORE_LONG].append(Datum(-78.1, time.time() + 5))
    plane.track.last_update = time.time() + 5
    store.write_live_planes({"ABC123": plane})
    assert fake.ttls[f"live:flight:{fid}"] == ttl
    assert fake.ttls[f"live:telemetry:{fid}"] == ttl


def test_reconnect_republishes_everything_and_drops_what_ended(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    assert store.claim_engine()
    plane = heading_plane("abc123", time.time(), samples=3)
    ended = heading_plane("def456", time.time())
    fid = plane.flight_id
    store.cache_episode(_episode(plane, "a1"))
    store.write_live_planes({"ABC123": plane, "DEF456": ended})
    # Redis restarts with its data (no AOF loss) while ``ended`` finishes...
    store._mark_disconnected(RuntimeError("restart"), store.client)
    fake.down = True
    store._last_connect_attempt = 0.0
    store.write_live_planes({"ABC123": plane})  # Redis down: nothing written
    fake.down = False
    # ... or loses some of it.
    fake.kv.pop(f"live:flight:{fid}")
    fake.zsets.clear()
    fake.ttls.clear()
    store._last_connect_attempt = 0.0
    assert store.ping()
    store.write_live_planes({"ABC123": plane})
    assert fake.sets["live:flights"] == {fid}
    assert f"live:flight:{ended.flight_id}" not in fake.kv
    assert len(fake.zsets[f"live:telemetry:{fid}"]) == 3
    assert "a1" in fake.hashes["live:active_alerts"]
    assert fake.ttls[f"live:flight:{fid}"] == keys.LIVE_FLIGHT_TTL_SECONDS
    assert fake.ttls[f"live:telemetry:{fid}"] == keys.LIVE_FLIGHT_TTL_SECONDS


# --- GNSS height in live telemetry -------------------------------------------


def test_live_telemetry_keeps_gnss_height(fake):
    store = LiveWriter(LOCAL_REDIS_URI, telemetry_keep_seconds=0)
    assert store.claim_engine()
    plane = gnss_then_baro_plane(time.time())
    first = plane.track.first_packet
    fid = f"abc123-{int(first)}"
    store.write_live_planes({"ABC123": plane})
    points = queries.get_telemetry(fid, "live", 0.0, live_store=store, history=None)
    assert points[0]["gnss_height"] == 10972.8
    assert "gnss_height" not in points[-1]
    assert any("gnss_height" in p for p in store.get_live_telemetry())


# --- Live telemetry reads are capped inside Redis ----------------------------


def _live_store_with_points(count: int) -> tuple[LiveWriter, str]:
    store = LiveWriter(LOCAL_REDIS_URI, telemetry_keep_seconds=0)
    store.claim_engine()
    plane = track_plane("abc123", 1_000.0, samples=count)
    store.write_live_planes({"ABC123": plane})
    store.touch_engine()
    return store, "abc123-1000"


def test_live_get_telemetry_limit_and_newest(fake):
    store, fid = _live_store_with_points(50)
    fake.calls.clear()
    newest = store.get_telemetry(fid, limit=10, newest=True)
    assert [p["timestamp"] for p in newest] == [1040.0 + n for n in range(10)]
    forward = store.get_telemetry(fid, since=1019.0, limit=5)
    assert [p["timestamp"] for p in forward] == [1020.0, 1021.0, 1022.0, 1023.0, 1024.0]
    newest_after = store.get_telemetry(fid, since=1019.0, limit=3, newest=True)
    assert [p["timestamp"] for p in newest_after] == [1047.0, 1048.0, 1049.0]
    assert len(store.get_telemetry(fid)) == 50  # no limit: unchanged
    # The cap is applied by Redis, not after reading the whole ring.
    assert "zrange" in fake.calls and "zrangebyscore" in fake.calls
    assert "zrevrangebyscore" in fake.calls


def test_live_fetch_telemetry_passes_the_cap_to_the_store(fake, monkeypatch):
    store, fid = _live_store_with_points(30)
    seen: list[dict] = []
    real = store.get_telemetry

    def spy(flight_id, **kwargs):
        seen.append(kwargs)
        result = real(flight_id, **kwargs)
        assert len(result) <= kwargs["limit"]
        return result

    monkeypatch.setattr(store, "get_telemetry", spy)
    newest = queries.get_telemetry(
        fid, "live", 0.0, live_store=store, history=None, limit=10
    )
    assert [p["timestamp"] for p in newest][-1] == 1029.0
    assert len(newest) == 10 and newest.truncated is True
    paged = queries.get_telemetry(
        fid, "live", 1004.0, live_store=store, history=None, limit=5
    )
    assert [p["timestamp"] for p in paged] == [1005.0, 1006.0, 1007.0, 1008.0, 1009.0]
    assert paged.truncated is True
    whole = queries.get_telemetry(
        fid, "live", 0.0, live_store=store, history=None, limit=30
    )
    assert len(whole) == 30 and whole.truncated is False
    default = queries.get_telemetry(fid, "live", 0.0, live_store=store, history=None)
    assert len(default) == 30
    assert seen[0] == {"since": 0.0, "limit": 11, "newest": True}
    assert seen[1] == {"since": 1004.0, "limit": 6, "newest": False}


# --- Latest live point -------------------------------------------------------


def test_latest_telemetry_reads_one_point(fake):
    store, fid = _live_store_with_points(40)
    fake.calls.clear()
    assert store.latest_telemetry(fid)["timestamp"] == 1039.0
    assert store.latest_telemetry("nope-1") is None
    assert "zrevrange" in fake.calls and "zrange" not in fake.calls


def test_live_flight_detail_uses_latest_point_only(fake, monkeypatch):
    store, fid = _live_store_with_points(40)

    def whole_ring(*_a, **_k):
        raise AssertionError("fetchFlight must not read the whole telemetry ring")

    monkeypatch.setattr(store, "get_telemetry", whole_ring)
    detail = queries.get_flight_detail(
        fid, "live", live_store=store, history=None, aircraft_db=None
    )
    assert detail["timestamp"] == 1039.0
    assert detail["latitude"] == pytest.approx(35.39)


# --- Score-less verdicts and telemetry provenance in the live store ----------


# --- /ws/live telemetry push carries provenance ------------------------------


def test_live_telemetry_push_keeps_provenance(fake):
    store = LiveWriter(LOCAL_REDIS_URI, telemetry_keep_seconds=0)
    plane = with_unknown_then_ok(track_plane("abc123", time.time()))
    plane.track.integrity_log.append(
        Verdict(
            plane.track.first_packet + 1.8,
            0.4,
            "spoofed",
            ("aoa",),
            {},
        )
    )
    store.write_live_planes({"ABC123": plane})
    points = store.get_live_telemetry()
    assert points[0]["receivers"] == ["roof", "mast"]
    assert points[0]["integrity_status"] == "unknown"
    assert points[-1]["integrity_status"] == "spoofed"
    assert points[-1]["integrity_score"] == 0.4
    assert points[-1]["integrity_flags"] == ["aoa"]
    # No verdict yet: the fields stay absent rather than null.
    store.write_live_planes({"DEF456": track_plane("def456", time.time())})
    bare = [p for p in store.get_live_telemetry() if p["icao"] == "def456"]
    assert "integrity_status" not in bare[0]


# --- Alert episodes in the tick's publish -----------------------------------


def test_writer_reads_redis_only_and_raises_when_down(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    plane = fine_track_plane("abc123", time.time())
    store.cache_episode(_episode(plane, "a1"))
    store.write_live_planes({"ABC123": plane})
    assert [a["alert_id"] for a in store.get_alerts(active_only=False)] == ["a1"]
    fake.fail.add("hvals")
    with pytest.raises(LiveUnavailable):
        store.get_alerts(flight_id=plane.flight_id, active_only=False)


def test_active_alert_refresh_is_batched_without_reads(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    plane = fine_track_plane("abc123", time.time())
    store.cache_episode(_episode(plane, "a1"))
    store.write_live_planes({"ABC123": plane})
    fake.calls.clear()
    for n in range(3):
        store.cache_episode(_episode(plane, "a1", last=10.0 + n))
    assert fake.calls == []  # nothing until the tick's publish
    store.write_live_planes({"ABC123": plane})
    assert "hget" not in fake.calls
    assert fake.calls.count("execute") == 1
    doc = json.loads(fake.hashes["live:active_alerts"]["a1"])
    assert doc["last_updated"] == 12.0 and doc["activated_at"] == 5.0


def test_deactivation_is_not_overwritten_by_a_pending_refresh(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    plane = fine_track_plane("abc123", time.time())
    store.cache_episode(_episode(plane, "a1"))
    store.cache_episode(_episode(plane, "a1", last=10.0))
    store.cache_episode(_episode(plane, "a1", active=False, last=11.0))
    store.write_live_planes({"ABC123": plane})
    assert "a1" not in fake.hashes.get("live:active_alerts", {})
    episode = json.loads(fake.hashes["live:alert_episodes"]["a1"])
    assert episode["active"] is False


def test_flight_no_longer_tracked_is_deleted_with_its_episodes(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    plane = fine_track_plane("abc123", time.time())
    other = fine_track_plane("def456", time.time())
    fid = plane.flight_id
    store.cache_episode(_episode(plane, "a1"))
    store.write_live_planes({"ABC123": plane, "DEF456": other})
    store.cache_episode(_episode(plane, "a1", active=False, last=11.0))
    store.write_live_planes({"DEF456": other})
    assert fake.sets["live:flights"] == {other.flight_id}
    for key in (f"live:flight:{fid}",):
        assert key not in fake.kv
    assert f"live:telemetry:{fid}" not in fake.zsets
    assert f"live:alerts:{fid}" not in fake.hashes
    assert "a1" not in fake.hashes.get("live:active_alerts", {})
    assert "a1" not in fake.hashes.get("live:alert_episodes", {})


def test_carried_episode_moves_to_the_next_segment(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    first = time.time() - 7_200
    plane = fine_track_plane("abc123", first)
    old = plane.flight_id
    store.cache_episode(_episode(plane, "A1"))
    store.write_live_planes({"ABC123": plane})
    plane.track.first_packet = first + 3_600
    new = plane.flight_id
    store.cache_episode(_episode(plane, "A1"))  # the handover re-records it
    store.write_live_planes({"ABC123": plane})
    assert fake.sets["live:flights"] == {new}
    assert f"live:alerts:{old}" not in fake.hashes
    assert json.loads(fake.hashes["live:alert_episodes"]["A1"])["flight_id"] == new
    assert json.loads(fake.hashes["live:active_alerts"]["A1"])["flight_id"] == new
    assert "A1" in fake.hashes[f"live:alerts:{new}"]


def test_episodes_of_flights_never_published_are_not_written(fake):
    """A previous process's flight closed at startup, or a flight closed after
    its last publish: no live keys appear for it."""
    store = LiveWriter(LOCAL_REDIS_URI)
    plane = fine_track_plane("abc123", 1_700_000_000.0)
    store.cache_episode(_episode(plane, "a1", active=False, last=9.0))
    store.write_live_planes({})
    assert not fake.hashes.get("live:alert_episodes")
    assert f"live:alerts:{plane.flight_id}" not in fake.hashes


def test_maintenance_handle_deletes_one_flight(fake):
    engine_store = LiveWriter(LOCAL_REDIS_URI)
    plane = fine_track_plane("abc123", time.time())
    engine_store.cache_episode(_episode(plane, "a1"))
    engine_store.write_live_planes({"ABC123": plane})
    handle = LiveWriter(LOCAL_REDIS_URI, writer=False)
    handle.pop_flight(plane.flight_id)
    assert not fake.sets.get("live:flights")
    assert "a1" not in fake.hashes.get("live:active_alerts", {})
