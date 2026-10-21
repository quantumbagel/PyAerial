"""Live store interface protocol shared across engine, API, and CLI components."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol

from pyaerial.constants import LIVE_ENGINE_TTL_SECONDS


class LiveStore(Protocol):
    def get_flights(self) -> list[dict[str, Any]]: ...

    def count_flights(self) -> int: ...

    def get_flight(self, flight_id: str) -> dict[str, Any] | None: ...

    def get_alerts(
        self,
        *,
        since: float = 0.0,
        flight_id: str | None = None,
        rule: str | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]: ...

    def get_telemetry(
        self, flight_id: str, *, since: float = 0.0
    ) -> list[dict[str, Any]]: ...

    def get_live_telemetry(self, since: float = 0.0) -> list[dict[str, Any]]: ...

    def ping(self) -> bool: ...

    def engine_seen_at(self) -> float | None: ...

    def engine_is_live(self) -> bool: ...

    def touch_engine(self) -> None: ...

    def clear_engine(self) -> None: ...

    def publish_raw(self, messages: list[dict[str, Any]]) -> None: ...

    def add_raw_listener(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]: ...

    def start_raw_pubsub(
        self, on_payload: Callable[[dict[str, Any]], None]
    ) -> None: ...

    def stop_raw_pubsub(self) -> None: ...

    def close(self) -> None: ...


def engine_is_live(live_store: Any, now: float | None = None) -> bool | None:
    """Whether the tracking engine is running, as seen through ``live_store``.

    ``True``/``False`` from a fresh / missing-or-stale ``live:engine``
    heartbeat. ``None`` when the store cannot be reached: a missing heartbeat
    is then *not* evidence that the engine is stopped. A ``None`` store means
    "no live store configured" and is treated as stopped.

    Duck-typed so test doubles need only ``engine_seen_at`` (and ``ping`` to
    model an outage).
    """
    if live_store is None:
        return False
    ping = getattr(live_store, "ping", None)
    if callable(ping) and not ping():
        return None
    getter = getattr(live_store, "engine_seen_at", None)
    seen = getter() if callable(getter) else None
    if not isinstance(seen, (int, float)):
        return False
    return (now if now is not None else time.time()) - seen < LIVE_ENGINE_TTL_SECONDS
