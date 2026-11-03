"""The tracking engine (``pyaerial run``).

- :mod:`.core`: :class:`Engine`, the tick loop and lifecycle, and :func:`run_engine`.
- :mod:`.supervisor`: :class:`ReceiverSupervisor`, receiver threads and the intake queue.
- :mod:`.archiver`: :class:`FlightArchiver`, archiving, recovery and segmentation.
- :mod:`.guard`: :class:`StageGuard`, shared stage guards and error counters.
"""

from pyaerial.engine.core import Engine, run_engine

__all__ = ["Engine", "run_engine"]
