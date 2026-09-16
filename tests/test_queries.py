from __future__ import annotations

from pyaerial.api.queries import get_stats, history_alert_filter, history_flight_filter
from pyaerial.api.payloads import FLIGHT_STATUS_LIVE
from pyaerial.store.redis_live import RedisLiveStore


def test_history_flight_filter_defaults():
    filt = history_flight_filter()
    assert filt["status"] == {"$ne": FLIGHT_STATUS_LIVE}
    assert "$or" in filt
    assert "end_time" not in filt


def test_history_flight_filter_range_and_search():
    filt = history_flight_filter(q="SWA*", since=100.0, until=200.0)
    assert filt["end_time"] == {"$gte": 100.0, "$lte": 200.0}
    and_clauses = filt["$and"]
    search = and_clauses[1]["$or"]
    pattern = search[0]["icao"]["$regex"]
    assert "*" not in pattern or pattern == r"SWA\*"
    assert search[0]["icao"]["$regex"] == r"SWA\*"


def test_history_alert_filter_q_and_until():
    filt = history_alert_filter(q="warn", since=10, until=50)
    assert filt["activated_at"] == {"$gte": 10, "$lte": 50}
    fields = {next(iter(item)) for item in filt["$or"]}
    assert fields == {"icao", "callsign", "zone", "rule", "flight_id"}


def test_get_stats_reports_store_health_and_engine_heartbeat():
    store = RedisLiveStore("redis://localhost:6379/0", memory_only=True)
    stats = get_stats(store, None)
    assert stats["live_flights"] == 0
    assert stats["redis"] is True
    assert stats["mongo"] is False
    assert stats["engine_seen_at"] is None
    store.touch_engine()
    stats = get_stats(store, None)
    assert isinstance(stats["engine_seen_at"], float)
    store.clear_engine()
    assert get_stats(store, None)["engine_seen_at"] is None


def test_get_stats_without_store():
    stats = get_stats(None, None)
    assert stats["redis"] is False
    assert stats["mongo"] is False
    assert stats["engine_seen_at"] is None

