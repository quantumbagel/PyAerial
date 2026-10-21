"""Live store factory for the CLI viewer."""

from __future__ import annotations

import logging
from typing import Any

from pyaerial.store.redis_live import RedisLiveStore, redact_uri

log = logging.getLogger("pyaerial.view")


def open_live_session(config: Any) -> RedisLiveStore:
    """Connect to the engine's Redis live store."""
    live_store = RedisLiveStore(config.database.redis_uri)
    if not live_store.ping():
        log.warning(
            "Redis unavailable at %s; live display will be empty until it reconnects.",
            redact_uri(config.database.redis_uri),
        )
    return live_store
