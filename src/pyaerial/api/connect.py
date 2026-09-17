"""Database and Redis connection helpers for the web portal."""

from __future__ import annotations

from pyaerial.config import load_config
from pyaerial.config.schema import Config
from pyaerial.store.history import HistoryStore
from pyaerial.store.redis_live import RedisLiveStore


def connect_stores(
    config_path: str,
) -> tuple[Config, HistoryStore, RedisLiveStore]:
    config = load_config(config_path)
    history = HistoryStore(config.database.path)
    live_store = RedisLiveStore(config.database.redis_uri)
    return config, history, live_store
