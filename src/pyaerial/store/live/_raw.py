"""In-process raw frame listeners and the ``live:raw`` Redis subscription.

The engine publishes batches (:meth:`LiveWriter.publish_raw`); the portal
subscribes. Local listeners also see local publishes, so a co-located
engine delivers in-process.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from collections.abc import Callable
from typing import Any

import redis

from pyaerial.store.live._conn import LiveConnection
from pyaerial.store.live.keys import RAW_CHANNEL

log = logging.getLogger("pyaerial.store.redis")

_RAW_PUBSUB_RETRY = 2.0


class RawFeed(LiveConnection):
    """Raw frame listeners plus a background ``live:raw`` subscriber thread."""

    def __init__(self, redis_uri: str) -> None:
        super().__init__(redis_uri)
        self._raw_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._raw_stop = threading.Event()
        self._raw_thread: threading.Thread | None = None
        self._raw_pubsub: Any = None
        self._raw_callback: Callable[[dict[str, Any]], None] | None = None

    def add_raw_listener(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Register an in-process listener for :meth:`publish_raw`."""
        self._raw_listeners.append(callback)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._raw_listeners.remove(callback)

        return remove

    def start_raw_pubsub(self, on_payload: Callable[[dict[str, Any]], None]) -> None:
        """Listen for ``live:raw`` on Redis and invoke *on_payload*.

        Always registers an in-process listener so a co-located engine's
        local publishes are seen too. A background thread subscribes to Redis.
        """
        if self._raw_callback is on_payload:
            return
        if self._raw_callback is not None:
            self.stop_raw_pubsub()
        self._raw_callback = on_payload
        self.add_raw_listener(on_payload)
        self._raw_stop.clear()
        self._raw_thread = threading.Thread(
            target=self._raw_pubsub_loop,
            name="pyaerial-raw-pubsub",
            daemon=True,
        )
        self._raw_thread.start()

    def stop_raw_pubsub(self) -> None:
        callback = self._raw_callback
        self._raw_callback = None
        if callback is not None:
            with contextlib.suppress(ValueError):
                self._raw_listeners.remove(callback)
        self._raw_stop.set()
        pubsub = self._raw_pubsub
        self._raw_pubsub = None
        if pubsub is not None:
            try:
                pubsub.close()
            except Exception as exc:
                log.debug("Error closing raw pub/sub: %s", exc)
        thread = self._raw_thread
        self._raw_thread = None
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)

    def _raw_pubsub_loop(self) -> None:
        while not self._raw_stop.is_set():
            client: redis.Redis | None = None
            pubsub = None
            try:
                client = redis.Redis.from_url(
                    self.uri,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(RAW_CHANNEL)
                self._raw_pubsub = pubsub
                while not self._raw_stop.is_set():
                    message = pubsub.get_message(timeout=1.0)
                    if not message or message.get("type") != "message":
                        continue
                    data = message.get("data")
                    try:
                        payload = json.loads(data)
                    except (TypeError, json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(payload, dict) and self._raw_callback is not None:
                        self._raw_callback(payload)
            except Exception:
                log.debug("Raw pub/sub listener reconnecting", exc_info=True)
            finally:
                self._raw_pubsub = None
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception as exc:
                        log.debug("Error closing raw pub/sub: %s", exc)
                if client is not None:
                    try:
                        client.close()
                    except Exception as exc:
                        log.debug("Error closing raw pub/sub client: %s", exc)
            if self._raw_stop.wait(_RAW_PUBSUB_RETRY):
                return

    def close(self) -> None:
        self.stop_raw_pubsub()
        super().close()
