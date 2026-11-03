"""LiveReader (the portal's live store) and the connection handling it
shares with the writer."""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest
import redis
from redis.exceptions import ConnectionError as RedisConnectionError

from fake_redis import FakeRedis
from helpers import LOCAL_REDIS_URI, alert_meta, track_plane
from pyaerial.constants import STORE_LAT, STORE_LONG
from pyaerial.models import AircraftInfo, Datum, Plane, TrackState
from pyaerial.store.codec import alert_episode_doc
from pyaerial.store.live import LiveReader, LiveUnavailable, LiveWriter, _conn
from pyaerial.urls import redact_uri

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


# --- Connection (shared with the writer) -------------------------------------


def test_redact_uri_hides_password():
    assert redact_uri(URI) == "redis://user:***@redis.example:6380/0"
    assert redact_uri("redis://:pw@h/0") == "redis://:***@h/0"
    assert redact_uri("redis://localhost:6379/0") == "redis://localhost:6379/0"


def test_connect_logs_never_contain_password(fake, caplog):
    fake.fail.add("ping")
    with caplog.at_level(logging.INFO, logger="pyaerial.store.redis"):
        LiveReader(URI)
        fake.fail.clear()
        LiveReader(URI)
    assert "s3cret" not in caplog.text
    assert "redis.example" in caplog.text


def test_reader_raises_live_unavailable_when_redis_down(fake):
    fake.down = True
    store = LiveReader(URI)
    with pytest.raises(LiveUnavailable):
        store.get_flights()
    with pytest.raises(LiveUnavailable):
        store.count_flights()


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, ["a1"]),
        ({"active_only": False}, ["a2", "a1"]),
        ({"flight_id": "FID"}, ["a1"]),
        ({"flight_id": "FID", "active_only": False}, ["a2", "a1"]),
        ({"rule": "warn", "active_only": False}, ["a2", "a1"]),
        ({"since": 1.5, "active_only": False}, ["a2"]),
    ],
)
def test_get_alerts_filters(fake, kwargs, expected):
    writer = LiveWriter(URI)
    plane = _plane(t=1_700_000_000.0)
    fid = f"abc123-{int(1_700_000_000.0)}"
    if kwargs.get("flight_id") == "FID":
        kwargs = {**kwargs, "flight_id": fid}
    meta = alert_meta()
    writer.cache_episode(
        alert_episode_doc(plane, meta, {}, alert_id="a1", activated_at=1.0, active=True)
    )
    writer.cache_episode(
        alert_episode_doc(
            plane, meta, {}, alert_id="a2", activated_at=2.0, active=False,
            deactivated_at=3.0,
        )
    )
    writer.write_live_planes({"ABC123": plane})
    reader = LiveReader(URI)
    got = [a["alert_id"] for a in reader.get_alerts(**kwargs)]
    assert sorted(got) == sorted(expected)
    assert [a["alert_id"] for a in writer.get_alerts(**kwargs)] == got


# --- Redis reconnect does not block other callers ----------------------------


class _SlowDownRedis(FakeRedis):
    """Redis host unreachable: every connect attempt takes a while to fail."""

    delay = 0.6

    def ping(self) -> bool:
        self.calls.append("ping")
        time.sleep(self.delay)
        raise RedisConnectionError("timed out")


def test_reconnect_does_not_block_other_threads(monkeypatch):
    client = _SlowDownRedis()
    attempts: list[int] = []

    def from_url(*_a, **_k):
        attempts.append(1)
        return client

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(from_url))
    store = LiveReader(LOCAL_REDIS_URI)  # a portal reader
    monkeypatch.setattr(_conn, "_RECONNECT_DELAY", 0.05)
    store._last_connect_attempt = 0.0
    attempts.clear()

    reconnecting = threading.Thread(target=store.ping)
    reconnecting.start()
    time.sleep(0.15)  # the reconnect is in flight and past its cooldown
    started = time.monotonic()
    with pytest.raises(LiveUnavailable):
        store.get_flights()
    assert store.ping() is False
    assert time.monotonic() - started < 0.2
    reconnecting.join()
    assert attempts == [1]  # one reconnect at a time


def test_reconnect_failure_is_still_reported(monkeypatch, caplog):
    client = _SlowDownRedis()
    client.delay = 0.0
    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda *a, **k: client))
    with caplog.at_level("WARNING", logger="pyaerial.store.redis"):
        store = LiveWriter(LOCAL_REDIS_URI)
    assert store.client is None
    assert "Redis unavailable" in caplog.text


# --- Read-only role -----------------------------------------------------------


def test_reader_has_no_write_methods(fake):
    reader = LiveReader(URI)
    for name in (
        "write_live_planes",
        "cache_episode",
        "touch_engine",
        "claim_engine",
        "ensure_writer",
        "clear_engine",
        "set_engine_health",
        "publish_raw",
        "pop_flight",
        "clear_all",
        "clear_cache",
        "save_outbox",
    ):
        assert not hasattr(reader, name), name


def test_reader_sees_what_the_writer_wrote(fake):
    writer = LiveWriter(LOCAL_REDIS_URI, telemetry_keep_seconds=0)
    assert writer.claim_engine()
    plane = track_plane("abc123", 1_000.0, samples=5)
    writer.cache_episode(
        alert_episode_doc(
            plane, alert_meta(), {}, alert_id="a1", activated_at=1.0, active=True
        )
    )
    writer.write_live_planes({"ABC123": plane})
    reader = LiveReader(LOCAL_REDIS_URI)
    fid = "abc123-1000"
    assert reader.get_flights() == writer.get_flights()
    assert reader.count_flights() == writer.count_flights() == 1
    assert reader.get_flight(fid) == writer.get_flight(fid)
    assert reader.get_telemetry(fid, limit=2, newest=True) == writer.get_telemetry(
        fid, limit=2, newest=True
    )
    assert reader.latest_telemetry(fid) == writer.latest_telemetry(fid)
    assert reader.get_live_telemetry() == writer.get_live_telemetry()
    assert reader.get_alerts() == writer.get_alerts()
    assert reader.engine_is_live() is True
    assert reader.engine_seen_at() == writer.engine_seen_at()


def test_reader_heartbeat_without_redis(fake):
    fake.down = True
    reader = LiveReader(LOCAL_REDIS_URI)
    assert reader.engine_seen_at() is None
    assert reader.engine_is_live() is False
    with pytest.raises(LiveUnavailable):
        reader.engine_heartbeat()


# --- engine_heartbeat --------------------------------------------------------


def test_engine_heartbeat_returns_the_whole_document(fake):
    writer = LiveWriter(LOCAL_REDIS_URI)
    writer.set_engine_health(history="ok", history_pending=2)
    assert writer.claim_engine()
    reader = LiveReader(LOCAL_REDIS_URI)
    fake.calls.clear()
    doc = reader.engine_heartbeat()
    assert fake.calls == ["get"]
    assert doc is not None
    assert doc["token"] == writer._engine_token
    assert doc["history"] == "ok" and doc["history_pending"] == 2
    assert doc == json.loads(fake.kv["live:engine"])


@pytest.mark.parametrize("stored", [None, "{not json", "[1, 2]"])
def test_engine_heartbeat_missing_or_unreadable_is_none(fake, stored):
    if stored is not None:
        fake.kv["live:engine"] = stored
    assert LiveReader(LOCAL_REDIS_URI).engine_heartbeat() is None


def test_engine_heartbeat_read_error_raises(fake):
    reader = LiveReader(LOCAL_REDIS_URI)
    fake.fail.add("get")
    with pytest.raises(LiveUnavailable):
        reader.engine_heartbeat()
