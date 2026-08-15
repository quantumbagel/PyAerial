"""Live store factory for the CLI viewer."""

from __future__ import annotations

import logging
from typing import Any

from pyaerial.calc.aircraft_db import AircraftDB
from pyaerial.mock_store import MockStore
from pyaerial.store.redis_live import RedisLiveStore

log = logging.getLogger("pyaerial.view")


def get_live_store(
    config: Any, mock: bool = False, aircraft_db: AircraftDB | None = None
) -> Any:
    if mock:
        return MockStore(aircraft_db=aircraft_db)
    live_store = RedisLiveStore(config.database.redis_uri)
    if live_store.client is None:
        log.warning(
            "Redis unavailable at %s; live display will be empty until it reconnects.",
            config.database.redis_uri,
        )
    return live_store
