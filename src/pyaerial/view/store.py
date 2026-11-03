"""Live store handle for ``pyaerial reset``."""

from __future__ import annotations

import logging
from typing import Any

from pyaerial.store.live.writer import LiveWriter
from pyaerial.urls import redact_uri

log = logging.getLogger("pyaerial.view")


def open_live_session(config: Any) -> LiveWriter:
    """Connect to the engine's Redis live store.

    A maintenance handle (``writer=False``): it can delete live flights but
    never claims ``live:engine`` or writes live keys, and its reads raise
    while Redis is down.
    """
    live_store = LiveWriter(config.database.redis_uri, writer=False)
    if not live_store.ping():
        log.warning(
            "Redis unavailable at %s; live display will be empty until it reconnects.",
            redact_uri(config.database.redis_uri),
        )
    return live_store
