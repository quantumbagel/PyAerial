"""The portal's read-only view of the Redis live store.

:class:`LiveReader` answers from Redis only (:class:`LiveQueries`), and
raises :class:`LiveUnavailable` when Redis cannot be read rather than
answer with nothing. It has no write methods; the engine's
:class:`~pyaerial.store.live.writer.LiveWriter` owns the keys.
"""

from __future__ import annotations

from pyaerial.store.live._queries import LiveQueries


class LiveReader(LiveQueries):
    """Read-only live store for the web portal (and other non-engine readers)."""

    def __init__(self, redis_uri: str) -> None:
        super().__init__(redis_uri)
        self._connect()
