from __future__ import annotations

import time

import pytest

from pyaerial.api.queries import (
    get_alerts,
    get_history_flights,
    get_live_flights,
    get_stats,
    get_telemetry,
)
from pyaerial.store.history import HistoryUnavailable
from pyaerial.store.redis_live import LiveUnavailable, RedisLiveStore


def test_get_stats_reports_store_health_and_engine_heartbeat():
    store = RedisLiveStore("redis://localhost:6379/0", memory_only=True)
    stats = get_stats(store, None)
    assert stats["live_flights"] == 0
    assert stats["redis"] is True
    assert stats["history"] is False
    assert stats["engine_seen_at"] is None
    store.touch_engine()
    stats = get_stats(store, None)
    assert isinstance(stats["engine_seen_at"], float)
    store.clear_engine()
    assert get_stats(store, None)["engine_seen_at"] is None


def test_reader_skips_redis_backfill():
    store = RedisLiveStore("redis://localhost:6379/0", memory_only=True, writer=False)
    store.client = object()
    store._backfill_redis_from_mem()


def test_memory_store_claim_engine_succeeds():
    store = RedisLiveStore("redis://localhost:6379/0", memory_only=True, writer=True)
    assert store.claim_engine() is True
    assert store.other_engine_is_live() is False


class _FakeRedis:
    def __init__(self):
        self.keys: dict[str, str] = {}

    def ping(self):
        return True

    def get(self, key):
        return self.keys.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.keys:
            return False
        self.keys[key] = value
        return True

    def delete(self, key):
        self.keys.pop(key, None)


def _writer_on_fake():
    store = RedisLiveStore("redis://localhost:6379/0", memory_only=True, writer=True)
    store.memory_only = False
    store.client = _FakeRedis()
    store._reported_down = False
    store._last_ping_ok = time.time() + 60
    return store


def test_claim_engine_nx_refuses_foreign_token():
    import json

    from pyaerial.constants import LIVE_ENGINE_TTL_SECONDS

    first = _writer_on_fake()
    shared = first.client
    assert first.claim_engine() is True
    second = _writer_on_fake()
    second.client = shared
    assert second.claim_engine() is False
    raw = json.loads(shared.get("live:engine"))
    assert raw["token"] == first._engine_token
    assert time.time() - raw["seen_at"] < LIVE_ENGINE_TTL_SECONDS


def test_touch_engine_does_not_overwrite_foreign_heartbeat():
    first = _writer_on_fake()
    assert first.claim_engine() is True
    owned = first.client.get("live:engine")
    second = _writer_on_fake()
    second.client = first.client
    second.touch_engine()
    assert first.client.get("live:engine") == owned


def test_ensure_writer_yields_to_foreign_token():
    first = _writer_on_fake()
    assert first.claim_engine() is True
    second = _writer_on_fake()
    second.client = first.client
    assert second.ensure_writer() is False
    assert second.writer is False
    assert first.ensure_writer() is True
    assert first.writer is True


def test_clear_engine_does_not_delete_foreign_heartbeat():
    first = _writer_on_fake()
    assert first.claim_engine() is True
    owned = first.client.get("live:engine")
    second = _writer_on_fake()
    second.client = first.client
    second.clear_engine()
    assert first.client.get("live:engine") == owned


def test_write_live_planes_skips_redis_after_yield():
    first = _writer_on_fake()
    assert first.claim_engine() is True
    second = _writer_on_fake()
    second.client = first.client
    assert second.ensure_writer() is False
    second.write_live_planes({"x": {}})
    assert first.client.get("live:engine") is not None


def test_history_queries_error_when_archive_down():
    class Down:
        def ping(self):
            return False

    down = Down()
    with pytest.raises(HistoryUnavailable):
        get_history_flights(down, None)
    with pytest.raises(HistoryUnavailable):
        get_alerts("history", live_store=None, history=down)
    with pytest.raises(HistoryUnavailable):
        get_telemetry("x", "history", 0.0, live_store=None, history=down)


def test_reader_get_flights_errors_when_redis_down():
    store = RedisLiveStore("redis://localhost:6379/0", memory_only=True, writer=False)
    store.memory_only = False
    store.client = None
    store._reported_down = True
    store._last_connect_attempt = time.monotonic()
    with pytest.raises(LiveUnavailable):
        store.get_flights()
    with pytest.raises(LiveUnavailable):
        get_live_flights(store, None)
    stats = get_stats(store, None)
    assert stats["redis"] is False
    assert stats["live_flights"] == 0


def test_memory_writer_get_flights_when_redis_down():
    store = RedisLiveStore("redis://localhost:6379/0", memory_only=True, writer=True)
    assert store.get_flights() == []


def test_reader_pop_flight_deletes_redis():
    store = RedisLiveStore("redis://localhost:6379/0", memory_only=True, writer=False)
    store.memory_only = False
    store.client = object()
    store._reported_down = False
    store._last_ping_ok = time.time() + 60
    deleted: list[str] = []
    store._ensure_connected = lambda: True  # type: ignore[method-assign]
    store._delete_redis_flight = lambda fid: deleted.append(fid) or {}  # type: ignore[method-assign]
    store.pop_flight("abc123-1")
    assert deleted == ["abc123-1"]


def test_get_stats_without_store():
    stats = get_stats(None, None)
    assert stats["redis"] is False
    assert stats["history"] is False
    assert stats["engine_seen_at"] is None
