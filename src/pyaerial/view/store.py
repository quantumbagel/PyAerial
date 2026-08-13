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
        log.info("Redis unavailable, using mock store for live data display.")
        return MockStore(aircraft_db=aircraft_db)
    return live_store
