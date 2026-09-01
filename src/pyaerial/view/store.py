"""Live store factory for the CLI viewer."""

from __future__ import annotations

import logging
from typing import Any

from pyaerial.constants import DEFAULT_AIRCRAFT_DB
from pyaerial.engine import start_isolated_engine
from pyaerial.store.redis_live import RedisLiveStore

log = logging.getLogger("pyaerial.view")


def open_live_session(
    config: Any,
    *,
    mock: bool = False,
    aircraft_db_path: str = DEFAULT_AIRCRAFT_DB,
) -> tuple[Any, Any]:
    """Return ``(live_store, engine_or_none)``.

    Mock mode starts an isolated tracking engine with a simulated ADS-B feed.
    """
    if mock:
        engine = start_isolated_engine(config, aircraft_db_path=aircraft_db_path)
        return engine.live_store, engine
    live_store = RedisLiveStore(config.database.redis_uri)
    if live_store.client is None:
        log.warning(
            "Redis unavailable at %s; live display will be empty until it reconnects.",
            config.database.redis_uri,
        )
    return live_store, None
