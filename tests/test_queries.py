from __future__ import annotations

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


def test_get_stats_without_store():
    stats = get_stats(None, None)
    assert stats["redis"] is False
    assert stats["history"] is False
    assert stats["engine_seen_at"] is None
