from __future__ import annotations

import time

from pyaerial.api.queries import get_stats
from pyaerial.store.redis_live import RedisLiveStore


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


def test_get_stats_without_store():
    stats = get_stats(None, None)
    assert stats["redis"] is False
    assert stats["history"] is False
    assert stats["engine_seen_at"] is None
