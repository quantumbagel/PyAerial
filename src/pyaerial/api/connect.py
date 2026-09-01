"""Database and Redis connection helpers for the web portal."""

from __future__ import annotations

import pymongo

from pyaerial.config import load_config
from pyaerial.config.schema import Config
from pyaerial.store.redis_live import RedisLiveStore


def connect_stores(
    config_path: str,
) -> tuple[Config, pymongo.MongoClient, pymongo.database.Database, RedisLiveStore]:
    config = load_config(config_path)
    client = pymongo.MongoClient(
        config.database.uri,
        serverSelectionTimeoutMS=2000,
        connectTimeoutMS=2000,
        socketTimeoutMS=5000,
    )
    if config.database.name:
        db = client.get_database(config.database.name)
    else:
        try:
            db = client.get_default_database()
        except Exception:
            db = client.get_database("pyaerial")
    live_store = RedisLiveStore(config.database.redis_uri)
    return config, client, db, live_store
