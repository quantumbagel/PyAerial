"""Stage guards: one stage's exception is logged and counted, the tick goes on."""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterator

# Every engine module logs as "pyaerial.engine", as engine.py did.
log = logging.getLogger("pyaerial.engine")

# A failing stage logs its traceback at most this often (seconds).
STAGE_ERROR_LOG_INTERVAL = 60.0


class StageGuard:
    """``with guard("stage"):`` contains an exception from that stage.

    The exception is logged with its traceback at most once per stage per
    :data:`STAGE_ERROR_LOG_INTERVAL` (later ones are counted and reported
    with the next traceback) and counted in :meth:`counts`, the heartbeat's
    ``stage_errors``. One instance is shared by the engine, the receiver
    supervisor and the flight archiver, so all three feed the same counter
    and rate limit. Tick thread only.
    """

    def __init__(self) -> None:
        # stage -> (monotonic time of the last logged traceback, suppressed count)
        self._last_logged: dict[str, tuple[float, int]] = {}
        self._counts: dict[str, int] = {}

    @contextlib.contextmanager
    def __call__(self, stage: str) -> Iterator[None]:
        """Contain an unexpected exception from one stage so the rest still runs."""
        try:
            yield
        except Exception:
            self.failed(stage)

    def failed(self, stage: str) -> None:
        """Log the exception being handled; one traceback per stage per interval."""
        self._counts[stage] = self._counts.get(stage, 0) + 1
        now = time.monotonic()
        last, suppressed = self._last_logged.get(stage, (float("-inf"), 0))
        if now - last < STAGE_ERROR_LOG_INTERVAL:
            self._last_logged[stage] = (last, suppressed + 1)
            return
        more = f" ({suppressed} more since the last report)" if suppressed else ""
        log.exception("Engine stage %r failed%s; continuing", stage, more)
        self._last_logged[stage] = (now, 0)

    def counts(self) -> dict[str, int]:
        """Failures per stage since start (a copy)."""
        return dict(self._counts)
