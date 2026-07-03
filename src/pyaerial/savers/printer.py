"""Saver that logs eligible flights instead of persisting them (useful for testing)."""
from __future__ import annotations

from pyaerial.savers import Saver, register_saver


@register_saver("print")
class PrintSaver(Saver):
    def save(self) -> None:
        if not self._cache:
            return
        self.logger.info("Would save %d eligible flight-level(s): %s",
                         len(self._cache), list(self._cache.keys()))
        for key, data in self._cache.items():
            self.logger.debug("  %s -> %s", key, data)
        self._cache = {}
