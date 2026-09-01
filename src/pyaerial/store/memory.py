"""In-memory live flight / telemetry / alert buffer."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


class MemoryLiveBuffer:
    """Fallback (and isolated-mode) copy of the live Redis documents."""

    def __init__(self) -> None:
        self.flights: dict[str, dict] = {}
        self.telemetry: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.alerts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.active_alerts: dict[str, dict[str, Any]] = {}
        self.alert_episodes: list[dict[str, Any]] = []

    def clear(self) -> None:
        self.flights.clear()
        self.telemetry.clear()
        self.alerts.clear()
        self.active_alerts.clear()
        self.alert_episodes.clear()
