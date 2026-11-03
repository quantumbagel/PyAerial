"""LiveWriter, the engine's live store: flight and telemetry writes, writer
claim, alert episodes, recovery reads and the memory mirror."""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from fake_redis import FakePipeline, FakeRedis
from helpers import (
    LOCAL_REDIS_URI,
    alert_doc,
    alert_meta,
    fine_track_plane,
    gnss_then_baro_plane,
    heading_plane,
    track_plane,
    with_unknown_then_ok,
)
from pyaerial.api import queries
from pyaerial.constants import (
    STORE_ICAO,
    STORE_LAT,
    STORE_LONG,
)
from pyaerial.models import (
    AircraftInfo,
    Datum,
    Plane,
    TrackState,
    Verdict,
    sample_provenance,
)
from pyaerial.store.codec import build_telemetry_docs
from pyaerial.store.live import LiveUnavailable, LiveWriter, keys, recovery

URI = "redis://user:s3cret@redis.example:6380/0"


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
    for n in range(20):
        store.write_live_planes({f"p{n}": _plane(f"a{n:05x}")})
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
    store.write_live_planes({"p": plane})
    store.record_alert_episode(plane, alert_meta(), {}, alert_id="a1", activated_at=1.0)
    assert "a1" in fake.hashes["live:active_alerts"]

    fake.fail.add("execute")
    store.record_alert_episode(
        plane, alert_meta(), {}, alert_id="a1", activated_at=1.0, active=False,
        deactivated_at=2.0,
    )
    assert store.client is None  # write failure was noticed
    assert "a1" in fake.hashes["live:active_alerts"]  # Redis still has the ghost

    fake.fail.clear()
    store._last_connect_attempt = 0.0
    assert store.ping()
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


def test_snapshot_keeps_sample_receivers_and_integrity(fake):
    store = LiveWriter(URI)
    t = time.time()
    plane = _plane(t=t)
    plane.received_data[STORE_LAT].append(Datum(35.1, t + 5))
    plane.received_data[STORE_LONG].append(Datum(-78.1, t + 5))
    plane.track.position_receivers = {t: ("nl", "be"), t + 5: ("nl",)}
    plane.track.integrity_log = [
        Verdict(t - 1, 0.9, "ok", (), {}),
        Verdict(t + 2, 0.3, "spoofed", ("aoa",), {}),
    ]
    store.write_live_planes({"abc123": plane})
    rebuilt = store.plane_for_finalize(f"abc123-{int(t)}")
    assert rebuilt is not None
    for stamp in (t, t + 5):
        assert sample_provenance(rebuilt, stamp) == sample_provenance(plane, stamp)


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
    store.write_live_planes({"ABC123": plane})
    store.record_alert_episode(
        plane, {STORE_ICAO: "abc123"}, {}, alert_id="a1", activated_at=1.0
    )
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


def test_reconnect_backfill_sets_ttls(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    assert store.claim_engine()
    plane = heading_plane("abc123", time.time())
    fid = f"abc123-{int(plane.track.first_packet)}"
    store.write_live_planes({"ABC123": plane})
    fake.kv.clear()
    fake.zsets.clear()
    fake.ttls.clear()
    store._mark_disconnected(RuntimeError("restart"), store.client)
    store._last_connect_attempt = 0.0
    assert store.ping()
    assert fake.ttls[f"live:flight:{fid}"] == keys.LIVE_FLIGHT_TTL_SECONDS
    assert fake.ttls[f"live:telemetry:{fid}"] == keys.LIVE_FLIGHT_TTL_SECONDS


def test_crash_recovery_still_rebuilds_flights_before_the_ttl(fake):
    crashed = LiveWriter(LOCAL_REDIS_URI)
    plane = heading_plane("abc123", time.time(), samples=3)
    crashed.write_live_planes({"ABC123": plane})
    fid = f"abc123-{int(plane.track.first_packet)}"

    restarted = LiveWriter(LOCAL_REDIS_URI)
    rebuilt = restarted.planes_for_finalize()
    assert [p.info.icao for p in rebuilt] == ["abc123"]
    assert len(rebuilt[0].received_data[STORE_LAT]) == 3
    assert fid in restarted.flight_ids()


def test_expired_flight_remnants_are_cleaned_not_retried(fake):
    crashed = LiveWriter(LOCAL_REDIS_URI)
    plane = heading_plane("abc123", time.time())
    fid = f"abc123-{int(plane.track.first_packet)}"
    crashed.write_live_planes({"ABC123": plane})
    crashed.record_alert_episode(
        plane, {STORE_ICAO: "abc123"}, {}, alert_id="a1", activated_at=1.0
    )
    # Engine stayed down past the TTL: per-flight keys are gone, but the
    # shared set and alert hashes still name the flight.
    fake.lapse(f"live:flight:{fid}", f"live:telemetry:{fid}", f"live:alerts:{fid}")

    restarted = LiveWriter(LOCAL_REDIS_URI)
    assert restarted.planes_for_finalize() == []
    assert fid not in fake.sets.get("live:flights", set())
    assert "a1" not in fake.hashes.get("live:active_alerts", {})
    assert "a1" not in fake.hashes.get("live:alert_episodes", {})
    fake.calls.clear()
    assert restarted.planes_for_finalize() == []
    assert "get" not in fake.calls  # nothing left to re-read every tick


def test_redis_error_during_snapshot_does_not_drop_the_flight(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    plane = heading_plane("abc123", time.time())
    fid = f"abc123-{int(plane.track.first_packet)}"
    store.write_live_planes({"ABC123": plane})
    store._mem.flights.clear()  # as if written by a previous process
    fake.fail.add("get")
    assert store.plane_for_finalize(fid) is None
    fake.fail.clear()
    store._last_connect_attempt = 0.0
    store.retry_pending_pops()
    assert fid in fake.sets["live:flights"]
    assert f"live:flight:{fid}" in fake.kv


# --- Capped telemetry reads, latest point, backfill and corrupt docs ---------


def test_live_telemetry_and_crash_snapshot_keep_gnss_height(fake):
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

    restarted = LiveWriter(LOCAL_REDIS_URI)
    rebuilt = restarted.plane_for_finalize(fid)
    docs = build_telemetry_docs(rebuilt, fid, "abc123")
    assert docs[0]["gnss_height"] == 10972.8
    assert "gnss_height" not in docs[-1]


# --- Live telemetry reads are capped inside Redis ----------------------------


def _live_store_with_points(
    count: int, *, memory_only: bool = False
) -> tuple[LiveWriter, str]:
    store = LiveWriter(
        LOCAL_REDIS_URI, memory_only=memory_only, telemetry_keep_seconds=0
    )
    store.claim_engine()
    plane = track_plane("abc123", 1_000.0, samples=count)
    store.write_live_planes({"ABC123": plane})
    store.touch_engine()
    return store, "abc123-1000"


@pytest.mark.parametrize("memory_only", [False, True])
def test_live_get_telemetry_limit_and_newest(fake, memory_only):
    store, fid = _live_store_with_points(50, memory_only=memory_only)
    fake.calls.clear()
    newest = store.get_telemetry(fid, limit=10, newest=True)
    assert [p["timestamp"] for p in newest] == [1040.0 + n for n in range(10)]
    forward = store.get_telemetry(fid, since=1019.0, limit=5)
    assert [p["timestamp"] for p in forward] == [1020.0, 1021.0, 1022.0, 1023.0, 1024.0]
    newest_after = store.get_telemetry(fid, since=1019.0, limit=3, newest=True)
    assert [p["timestamp"] for p in newest_after] == [1047.0, 1048.0, 1049.0]
    assert len(store.get_telemetry(fid)) == 50  # no limit: unchanged
    if not memory_only:
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


@pytest.mark.parametrize("memory_only", [False, True])
def test_latest_telemetry_reads_one_point(fake, memory_only):
    store, fid = _live_store_with_points(40, memory_only=memory_only)
    fake.calls.clear()
    assert store.latest_telemetry(fid)["timestamp"] == 1039.0
    assert store.latest_telemetry("nope-1") is None
    if not memory_only:
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


# --- Backfill snapshots memory under the lock --------------------------------


def test_backfill_survives_concurrent_live_writes(fake, monkeypatch):
    store = LiveWriter(LOCAL_REDIS_URI, telemetry_keep_seconds=0)
    assert store.claim_engine()
    for n in range(5):
        store.write_live_planes({f"P{n}": track_plane(f"a{n:05d}", 1_000.0 + n)})
    store._mark_disconnected(RuntimeError("restart"), store.client)
    fake.kv.clear()
    fake.zsets.clear()
    fake.sets.clear()

    real_zadd = FakePipeline.__getattr__
    writers: list[threading.Thread] = []

    def queue_and_mutate(self, name):
        method = real_zadd(self, name)
        if name == "zadd" and not writers:
            # The engine thread adds a flight while the backfill is queueing.
            writer = threading.Thread(
                target=store.write_live_planes,
                args=({"NEW": track_plane("bbbbbb", 2_000.0)},),
            )
            writers.append(writer)
            writer.start()
            writer.join(timeout=2.0)
            assert not writer.is_alive(), "live writer blocked behind the backfill"
        return method

    monkeypatch.setattr(FakePipeline, "__getattr__", queue_and_mutate)
    store._last_connect_attempt = 0.0
    assert store.ping() is True
    assert writers
    assert {f"a{n:05d}-{1000 + n}" for n in range(5)} <= fake.sets["live:flights"]


# --- Corrupt live docs are not re-read every sweep ---------------------------


def test_corrupt_live_doc_is_skipped_after_first_sweep(fake, caplog):
    fake.sets["live:flights"] = {"bad111-1", "noicao-1"}
    fake.kv["live:flight:bad111-1"] = "{not json"
    fake.kv["live:flight:noicao-1"] = '{"info": {}}'
    store = LiveWriter(LOCAL_REDIS_URI)
    with caplog.at_level(logging.WARNING, logger="pyaerial.store.redis"):
        assert store.planes_for_finalize() == []
        fake.calls.clear()
        assert store.planes_for_finalize() == []
    assert fake.calls.count("get") == 0
    assert caplog.text.count("bad111-1") == 1
    assert caplog.text.count("noicao-1") == 1
    # Kept (not deleted), but the TTL guarantees Redis lets go eventually.
    assert fake.kv["live:flight:bad111-1"] == "{not json"
    assert fake.ttls["live:flight:bad111-1"] > 0


def test_unreadable_flight_memory_is_bounded(fake, monkeypatch):
    monkeypatch.setattr(recovery, "_UNREADABLE_FLIGHTS_MAX", 3)
    store = LiveWriter(LOCAL_REDIS_URI)
    for n in range(5):
        fake.kv[f"live:flight:x{n}-1"] = "garbage"
        store.plane_for_finalize(f"x{n}-1")
    assert len(store._unreadable_flights) == 3


# --- Score-less verdicts and telemetry provenance in the live store ----------


def test_live_snapshot_rebuilds_scoreless_verdicts(fake):
    store = LiveWriter(LOCAL_REDIS_URI, telemetry_keep_seconds=0)
    plane = with_unknown_then_ok(track_plane("abc123", time.time()))
    fid = f"abc123-{int(plane.track.first_packet)}"
    store.write_live_planes({"ABC123": plane})
    restarted = LiveWriter(LOCAL_REDIS_URI)
    rebuilt = restarted.plane_for_finalize(fid)
    verdicts = rebuilt.track.integrity_log
    assert [(v.score, v.status) for v in verdicts] == [(None, "unknown"), (0.9, "ok")]
    original = build_telemetry_docs(plane, fid, "abc123")
    again = build_telemetry_docs(rebuilt, fid, "abc123")
    assert [d.get("integrity_status") for d in again] == [
        d.get("integrity_status") for d in original
    ]


# --- /ws/live telemetry push carries provenance ------------------------------


@pytest.mark.parametrize("memory_only", [False, True])
def test_live_telemetry_push_keeps_provenance(fake, memory_only):
    store = LiveWriter(
        LOCAL_REDIS_URI, memory_only=memory_only, telemetry_keep_seconds=0
    )
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
    bare = LiveWriter(LOCAL_REDIS_URI, memory_only=True, telemetry_keep_seconds=0)
    bare.write_live_planes({"DEF456": track_plane("def456", time.time())})
    assert "integrity_status" not in bare.get_live_telemetry()[0]


# --- Alert episodes in Redis and batched refreshes ---------------------------


def test_get_alerts_merges_redis_and_memory_episodes(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    plane = fine_track_plane("abc123", 1_700_000_000.0)
    fid = plane.flight_id
    fake.hashes[f"live:alerts:{fid}"] = {
        "earlier": alert_doc("earlier", fid, active=False, at=1_700_000_010.0),
        "current": alert_doc("current", fid, active=True, at=1_700_000_100.0),
    }
    # The restart closes the active one, which puts the flight into memory.
    store.record_alert_episode(
        plane,
        alert_meta(),
        {},
        alert_id="current",
        activated_at=1_700_000_100.0,
        active=False,
        deactivated_at=1_700_000_200.0,
    )
    alerts = {
        a["alert_id"]: a for a in store.get_alerts(flight_id=fid, active_only=False)
    }
    assert set(alerts) == {"earlier", "current"}
    assert alerts["current"]["active"] is False  # memory wins


def test_writer_raises_instead_of_returning_no_alerts(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    fid = "abc123-1700000000"
    fake.hashes[f"live:alerts:{fid}"] = {
        "a1": alert_doc("a1", fid, active=False, at=1.0)
    }
    fake.fail.add("hvals")
    with pytest.raises(LiveUnavailable):
        store.get_alerts(flight_id=fid, active_only=False)
    # Redis known down (cooldown): a flight this process never owned cannot
    # be answered from memory either.
    fake.fail.clear()
    store._last_connect_attempt = time.monotonic()  # within the reconnect cooldown
    assert store.client is None
    with pytest.raises(LiveUnavailable):
        store.get_alerts(flight_id=fid, active_only=False)
    store._last_connect_attempt = 0.0
    # Its own flights are fully in memory.
    plane = fine_track_plane("def456", 1_700_000_000.0)
    store.write_live_planes({"DEF456": plane})
    store.record_alert_episode(
        plane, alert_meta("def456"), {}, alert_id="b1", activated_at=2.0
    )
    assert [
        a["alert_id"]
        for a in store.get_alerts(
            flight_id=plane.flight_id, active_only=False
        )
    ] == ["b1"]


# --- Active alert refresh: no HGET, batched with the tick --------------------


def test_active_alert_refresh_is_batched_without_reads(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    plane = fine_track_plane("abc123", time.time())
    store.write_live_planes({"ABC123": plane})
    store.record_alert_episode(plane, alert_meta(), {}, alert_id="a1", activated_at=5.0)
    fake.calls.clear()
    for n in range(3):
        store.update_active_alert(plane, "a1", alert_meta(), {}, 10.0 + n)
    assert fake.calls == []  # nothing until the tick's flush
    store.write_live_planes({"ABC123": plane})
    assert "hget" not in fake.calls
    assert fake.calls.count("execute") == 1
    doc = json.loads(fake.hashes["live:active_alerts"]["a1"])
    assert doc["last_updated"] == 12.0 and doc["activated_at"] == 5.0


def test_deactivation_is_not_overwritten_by_a_pending_refresh(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    plane = fine_track_plane("abc123", time.time())
    store.record_alert_episode(plane, alert_meta(), {}, alert_id="a1", activated_at=5.0)
    store.update_active_alert(plane, "a1", alert_meta(), {}, 10.0)
    store.record_alert_episode(
        plane,
        alert_meta(),
        {},
        alert_id="a1",
        activated_at=5.0,
        active=False,
        deactivated_at=11.0,
    )
    store.write_live_planes({"ABC123": plane})
    assert "a1" not in fake.hashes.get("live:active_alerts", {})
    episode = json.loads(fake.hashes["live:alert_episodes"]["a1"])
    assert episode["active"] is False


def test_popped_flight_refresh_is_dropped(fake):
    store = LiveWriter(LOCAL_REDIS_URI)
    plane = fine_track_plane("abc123", time.time())
    store.record_alert_episode(plane, alert_meta(), {}, alert_id="a1", activated_at=5.0)
    store.update_active_alert(plane, "a1", alert_meta(), {}, 10.0)
    store.pop_flight(plane.flight_id)
    store.write_live_planes({"DEF456": fine_track_plane("def456", time.time())})
    assert "a1" not in fake.hashes.get("live:active_alerts", {})
