"""The live store of an isolated engine (``Engine(isolated=True)``, the
replay and profiling tools): nothing is published, raw frames reach only
in-process listeners."""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from pyaerial.models import Plane

log = logging.getLogger("pyaerial.store.redis")


class NullLiveWriter:
    """A :class:`~pyaerial.store.live.LiveWriteStore` that writes nothing."""

    writer = True

    def __init__(self) -> None:
        self._raw_listeners: list[Callable[[dict[str, Any]], None]] = []
        self.engine_health: dict[str, Any] = {}

    def ping(self) -> bool:
        return True

    def claim_engine(self) -> bool:
        return True

    def ensure_writer(self) -> bool:
        return True

    def touch_engine(self) -> None:
        pass

    def clear_engine(self) -> None:
        pass

    def set_engine_health(self, **fields: Any) -> None:
        self.engine_health = dict(fields)

    def write_live_planes(self, planes: Mapping[str, Plane]) -> None:
        pass

    def cache_episode(self, doc: Mapping[str, Any]) -> None:
        pass

    def save_outbox(self, entries: Mapping[str, Any]) -> None:
        pass

    def drop_outbox(self, alert_ids: Iterable[str]) -> None:
        pass

    def load_outbox(self) -> list[Any]:
        return []

    def replace_outbox(self, entries: Mapping[str, Any]) -> None:
        pass

    def clear_cache(self) -> int:
        return 0

    def add_raw_listener(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Register an in-process listener for :meth:`publish_raw`."""
        self._raw_listeners.append(callback)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._raw_listeners.remove(callback)

        return remove

    def publish_raw(self, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        payload = {"timestamp": time.time(), "messages": messages}
        for listener in list(self._raw_listeners):
            try:
                listener(payload)
            except Exception:
                log.debug("Raw-frame listener failed", exc_info=True)

    def close(self) -> None:
        pass
