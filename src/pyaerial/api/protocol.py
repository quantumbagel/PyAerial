"""Live and mock store interfaces used by the web portal."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LiveStore(Protocol):
    def get_flights(self) -> list[dict[str, Any]]: ...

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

    def close(self) -> None: ...
