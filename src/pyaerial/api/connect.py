"""Database and Redis connection helpers for the web portal."""

from __future__ import annotations

from pyaerial.config import load_config
from pyaerial.config.schema import Config
from pyaerial.store.history import HistoryStore
from pyaerial.store.redis_live import RedisLiveStore


def open_portal_history(config: Config) -> HistoryStore:
    """The archive as the portal sees it: read-only, never created or upgraded.

    Only the engine writes (and upgrades) the archive. Opening it read-only
    also means the portal never waits for the engine's write lock.
    """
    return HistoryStore(config.database.path, read_only=True)


def open_portal_stores(config: Config) -> tuple[HistoryStore, RedisLiveStore]:
    return open_portal_history(config), RedisLiveStore(config.database.redis_uri)


def connect_stores(
    config_path: str,
) -> tuple[Config, HistoryStore, RedisLiveStore]:
    config = load_config(config_path)
    history, live_store = open_portal_stores(config)
    return config, history, live_store
