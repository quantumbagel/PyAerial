from __future__ import annotations

import logging
import time

import pytest
import redis

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
from pyaerial.models import Datum
from pyaerial.store.redis_live import LiveUnavailable, RedisLiveStore, redact_uri
from fake_redis import FakeRedis

URI = "redis://user:s3cret@redis.example:6380/0"


@pytest.fixture
def fake(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda *a, **k: client))
    return client


def _plane(icao: str = "abc123", t: float | None = None) -> dict:
    t = time.time() if t is None else t
    return {
        STORE_INFO: {STORE_ICAO: icao},
        STORE_INTERNAL: {STORE_FIRST_PACKET: t, STORE_MOST_RECENT_PACKET: t},
        STORE_RECV_DATA: {
            STORE_LAT: [Datum(35.0, t)],
            STORE_LONG: [Datum(-78.0, t)],
        },
    }


def _meta(icao: str = "abc123") -> dict:
    return {STORE_ICAO: icao, "zone": "pad", "type": "warn"}


def test_redact_uri_hides_password():
    assert redact_uri(URI) == "redis://user:***@redis.example:6380/0"
    assert redact_uri("redis://:pw@h/0") == "redis://:***@h/0"
    assert redact_uri("redis://localhost:6379/0") == "redis://localhost:6379/0"


def test_connect_logs_never_contain_password(fake, caplog):
    fake.fail.add("ping")
    with caplog.at_level(logging.INFO, logger="pyaerial.store.redis"):
        RedisLiveStore(URI)
        fake.fail.clear()
        RedisLiveStore(URI)
    assert "s3cret" not in caplog.text
    assert "redis.example" in caplog.text


def test_get_flights_batches_round_trips_and_count_uses_scard(fake):
    store = RedisLiveStore(URI, writer=True)
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
    store = RedisLiveStore(URI, writer=True)
    fake.fail.add("execute")
    store.write_live_planes({"p": _plane()})
    assert store.client is None


def test_failed_deactivation_does_not_leave_ghost_active_alert(fake):
    store = RedisLiveStore(URI, writer=True)
    assert store.claim_engine()
    plane = _plane()
    store.write_live_planes({"p": plane})
    store.record_alert_episode(plane, _meta(), {}, alert_id="a1", activated_at=1.0)
    assert "a1" in fake.hashes["live:active_alerts"]

    fake.fail.add("execute")
    store.record_alert_episode(
        plane, _meta(), {}, alert_id="a1", activated_at=1.0, active=False,
        deactivated_at=2.0,
    )
    assert store.client is None  # write failure was noticed
    assert "a1" in fake.hashes["live:active_alerts"]  # Redis still has the ghost

    fake.fail.clear()
    store._last_connect_attempt = 0.0
    assert store.ping()
    assert "a1" not in fake.hashes["live:active_alerts"]


def test_stale_disconnect_does_not_drop_fresh_client(fake):
    store = RedisLiveStore(URI, writer=True)
    old = store.client
    replacement = FakeRedis()
    store.client = replacement
    store._mark_disconnected(RuntimeError("late"), old)
    assert store.client is replacement


def test_reader_raises_live_unavailable_when_redis_down(fake):
    fake.down = True
    store = RedisLiveStore(URI, writer=False)
    with pytest.raises(LiveUnavailable):
        store.get_flights()
    with pytest.raises(LiveUnavailable):
        store.count_flights()


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"active_only": False},
        {"flight_id": "FID"},
        {"flight_id": "FID", "active_only": False},
        {"rule": "warn", "active_only": False},
        {"since": 1.5, "active_only": False},
    ],
)
def test_get_alerts_memory_and_redis_agree(fake, kwargs):
    writer = RedisLiveStore(URI, writer=True)
    mem = RedisLiveStore(URI, memory_only=True, writer=True)
    plane = _plane(t=1_700_000_000.0)
    fid = f"abc123-{int(1_700_000_000.0)}"
    if kwargs.get("flight_id") == "FID":
        kwargs = {**kwargs, "flight_id": fid}
    for store in (writer, mem):
        store.record_alert_episode(plane, _meta(), {}, alert_id="a1", activated_at=1.0)
        store.record_alert_episode(plane, _meta(), {}, alert_id="a2", activated_at=2.0)
        store.record_alert_episode(
            plane, _meta(), {}, alert_id="a2", activated_at=2.0, active=False,
            deactivated_at=3.0,
        )
    reader = RedisLiveStore(URI, writer=False)
    from_redis = [a["alert_id"] for a in reader.get_alerts(**kwargs)]
    from_mem = [a["alert_id"] for a in mem.get_alerts(**kwargs)]
    assert from_redis == from_mem
    assert from_redis


def test_unchanged_flights_are_not_rewritten(fake):
    store = RedisLiveStore(URI, writer=True)
    planes = {f"p{n}": _plane(f"a{n:05x}", t=1000.0) for n in range(5)}
    store.write_live_planes(planes)
    assert fake.calls.count("execute") == 1  # one round trip for all five
    fake.calls.clear()
    store.write_live_planes(planes)
    assert fake.calls.count("execute") == 0
    planes["p0"][STORE_INFO]["callsign"] = "NEW1"
    store.write_live_planes(planes)
    assert fake.calls.count("execute") == 1
    assert '"NEW1"' in fake.kv["live:flight:a00000-1000"]
