"""Redis live store: live flights, telemetry rings, alert episodes, the
engine heartbeat and the raw frame channel.

- :class:`~pyaerial.store.live.writer.LiveWriter`: the engine (and
  ``pyaerial reset``); owns the keys, claims ``live:engine``, publishes what
  it tracks as a cache. Import it from its module: the package does not
  export it, so the portal never loads it.
- :class:`NullLiveWriter`: an isolated engine's; writes nothing.
- :class:`LiveReader`: the portal; read-only queries, heartbeat and raw
  subscription.
- :mod:`~pyaerial.store.live.keys`: key names, TTLs, heartbeat payload.
- :mod:`~pyaerial.store.live.protocols`: what api/, alerts and the engine
  depend on.
"""

from pyaerial.store.live._conn import LiveUnavailable
from pyaerial.store.live.protocols import (
    EpisodeStore,
    LiveReadStore,
    LiveWriteStore,
    engine_is_live,
)
from pyaerial.store.live.null import NullLiveWriter
from pyaerial.store.live.reader import LiveReader

__all__ = [
    "EpisodeStore",
    "LiveReadStore",
    "LiveReader",
    "LiveUnavailable",
    "LiveWriteStore",
    "NullLiveWriter",
    "engine_is_live",
]
