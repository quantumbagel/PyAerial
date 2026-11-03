"""Database and Redis connection helpers for the web portal."""

from __future__ import annotations

from pyaerial.config import load_config
from pyaerial.config.schema import Config
from pyaerial.store.archive import ArchiveReader
from pyaerial.store.live import LiveReader


def open_portal_history(config: Config) -> ArchiveReader:
    """The archive as the portal sees it: read-only, never created or upgraded.

    Only the engine writes (and upgrades) the archive. Opening it read-only
    also means the portal never waits for the engine's write lock.
    """
    return ArchiveReader(config.database.path)


def open_portal_stores(config: Config) -> tuple[ArchiveReader, LiveReader]:
    return open_portal_history(config), LiveReader(config.database.redis_uri)


def connect_stores(
    config_path: str,
) -> tuple[Config, ArchiveReader, LiveReader]:
    config = load_config(config_path)
    history, live_store = open_portal_stores(config)
    return config, history, live_store
