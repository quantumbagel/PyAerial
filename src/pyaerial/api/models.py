"""Wire models of the ``/ws/live`` protocol (see WEBSOCKET.md).

These pydantic models are the reference for what the portal sends. The
payload builders (:mod:`pyaerial.api.payloads`, :mod:`pyaerial.api.queries`,
the broadcaster) still build plain dicts: the broadcaster serializes every
tick, and dict building keeps both the bytes and their cost unchanged.
``tests/test_wire_models.py`` validates the builders' output against these
models, strictly and with unknown keys forbidden. ``scripts/gen_types.py``
turns :func:`wire_json_schema` into ``web/src/api/schema.json`` and the
portal's ``web/src/api/generated.ts``.

Conventions:

* A field without a default is always sent; ``X | None`` means it may be
  ``null``.
* A field declared with :func:`omittable` is left out of the message when it
  has no value (it is never ``null`` unless its type says so).
  :func:`dump` (``exclude_unset``) reproduces that, and the JSON schema leaves
  it out of ``required``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel
from pydantic.json_schema import models_json_schema

IntegrityStatus = Literal["ok", "suspect", "spoofed", "unknown"]
CheckStatus = Literal["pass", "warn", "fail"]
FlightStatus = Literal["live", "completed"]
StreamName = Literal["flights", "alerts", "telemetry", "stats"]
ActionName = Literal[
    "subscribe",
    "fetchFlights",
    "fetchFlight",
    "fetchTelemetry",
    "fetchAlerts",
    "fetchStats",
    "fetchZones",
    "fetchConfig",
]
ErrorCode = Literal[
    "bad_request",
    "not_found",
    "rate_limited",
    "history_unavailable",
    "live_unavailable",
    "internal_error",
]


def _no_default(schema: dict[str, Any]) -> None:
    schema.pop("default", None)


def omittable() -> Any:
    """Default for a key that is left out of the message when it has no value."""
    return Field(default=None, json_schema_extra=_no_default)


def dump(model: BaseModel) -> dict[str, Any]:
    """The message as sent: omittable keys that were never set are left out."""
    return model.model_dump(mode="json", exclude_unset=True)


class WireModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        use_attribute_docstrings=True,
    )


# -- shared pieces -------------------------------------------------------------


class LatLon(WireModel):
    """A WGS84 position in degrees."""

    latitude: float
    longitude: float


class AlertStats(WireModel):
    """Alert episodes of one flight."""

    episode_count: int
    total_seconds: int
    """Seconds spent in alert, summed over episodes."""
    active_count: int


class ActiveAlert(WireModel):
    """An episode in progress on a flight (``active_alerts[]``)."""

    alert_id: str
    """Never empty: ``<flight_id>:<zone>:<rule>`` when the store has no id."""
    zone: str
    rule: str
    activated_at: float
    eta: float | None


# -- integrity -----------------------------------------------------------------


class IntegrityCheck(WireModel):
    """One integrity check that had evidence."""

    status: CheckStatus
    detail: str
    value: float = omittable()
    """The measured quantity, when the check has one (rounded to 0.01)."""


class Emitter(WireModel):
    """Multilaterated transmitter location, for targets that fail TDOA."""

    latitude: float
    longitude: float
    offset_km: float
    """Ground distance from the claimed position."""


class Integrity(WireModel):
    """Spoofing checks from the engine's integrity monitor (see CONFIGURATION.md)."""

    score: float | None
    """0-1; 1 means consistent with every check that had evidence. Null while
    ``unknown``."""
    status: IntegrityStatus
    checks: dict[str, IntegrityCheck]
    emitter: Emitter = omittable()
    rebroadcast: bool = omittable()
    """Only ground rebroadcasts (TIS-B/ADS-R) heard lately: receiver RF checks
    don't apply. Sent only as ``true``."""


# -- flights -------------------------------------------------------------------


class _FlightBase(WireModel):
    flight_id: str
    icao: str
    active_alerts: list[ActiveAlert]
    start_time: float
    end_time: float
    callsign: str | None
    model: str | None
    owner: str | None
    country: str | None
    aircraft_type: str | None
    registration: str | None
    integrity: Integrity | None
    """Null until the engine scores the aircraft."""
    is_live: bool
    status: FlightStatus


class FlightSummary(_FlightBase):
    """A flight in the ``flights`` stream and ``fetchFlights`` (live and history)."""

    latitude: float | None
    longitude: float | None
    altitude: float | None
    """Barometric, metres."""
    speed: float | None
    """km/h."""
    heading: float | None
    retained: bool
    timestamp: float | None
    """Time of the newest position; null without telemetry."""
    alert_stats: AlertStats = omittable()


class FlightDetail(_FlightBase):
    """``fetchFlight``: a flight with airframe metadata and photo links.

    The live view leaves out the position fields while the flight has no
    telemetry, and never sends ``retained``.
    """

    latitude: float | None = omittable()
    longitude: float | None = omittable()
    altitude: float | None = omittable()
    speed: float | None = omittable()
    heading: float | None = omittable()
    timestamp: float | None = omittable()
    retained: bool = omittable()
    alert_stats: AlertStats | None = omittable()
    """History sends ``null`` for a flight without alert episodes."""
    photo_url: str | None
    photo_photographer: str | None
    photo_link: str | None


class TelemetryPoint(WireModel):
    """A track point, pushed on ``telemetry`` or returned by ``fetchTelemetry``."""

    flight_id: str
    timestamp: float
    icao: str | None = omittable()
    """Pushed points only."""
    latitude: float | None = omittable()
    longitude: float | None = omittable()
    altitude: float | None
    """Barometric, metres."""
    gnss_height: float = omittable()
    """Metres above the WGS84 ellipsoid (GNSS, TC 20-22 only)."""
    speed: float | None
    heading: float | None
    receivers: list[str] = omittable()
    """Receivers that heard this position, strongest copy first."""
    integrity_score: float | None = omittable()
    """Integrity verdict in effect when the sample arrived (null while unknown)."""
    integrity_status: IntegrityStatus = omittable()
    integrity_flags: list[str] = omittable()
    """Checks that did not pass in that verdict."""


# -- alerts ----------------------------------------------------------------------


class Alert(WireModel):
    """An alert episode (``alerts`` stream, ``fetchAlerts``)."""

    alert_id: str
    flight_id: str
    icao: str | None
    callsign: str | None
    zone: str
    rule: str
    active: bool
    activated_at: float | None
    deactivated_at: float | None
    ended_by: str | None
    """Why an ended episode ended: cleared | lost | shutdown | restart. Null while
    active or unknown."""
    eta: float | None
    altitude: float | None
    latitude: float | None
    longitude: float | None


# -- stats, zones, config ----------------------------------------------------------


class ServerStats(WireModel):
    """The ``stats`` stream and ``fetchStats``."""

    live_flights: int
    active_alerts: int
    retained_flights: int
    historical_alerts: int
    redis: bool
    history: bool
    engine_seen_at: float | None
    """Newest engine heartbeat (unix seconds); null if none was ever written."""
    engine_live: bool
    """Server-computed heartbeat freshness; prefer it to the browser clock."""


class Bound(WireModel):
    min: float = omittable()
    max: float = omittable()


class ZoneRule(WireModel):
    name: str
    when: dict[str, Bound]
    dwell_seconds: float
    color: str = omittable()


class Zone(WireModel):
    name: str
    coordinates: list[tuple[float, float]]
    """``[latitude, longitude]`` pairs."""
    rules: list[ZoneRule]
    color: str = omittable()


class ZonesData(WireModel):
    """``fetchZones``."""

    home: LatLon
    zones: list[Zone]
    alert_colors: dict[str, str]


class ReceiverPosition(WireModel):
    latitude: float
    longitude: float
    altitude: float
    """Metres above mean sea level."""


class ReceiverInfo(WireModel):
    """A configured receiver. Hosts and ports are never sent."""

    name: str
    type: str
    position: ReceiverPosition = omittable()
    format: str = omittable()
    """Wire format (``beast``, ``avr``, ...), when known."""


class AppConfig(WireModel):
    """``fetchConfig``."""

    home: LatLon
    remember_planes: float
    receivers: list[ReceiverInfo]
    beast_websocket: str
    beast_host: str = omittable()
    """Beast TCP listener, only when it is on."""
    beast_port: int = omittable()


class SubscribeResult(WireModel):
    streams: list[StreamName]


# -- pushed messages -------------------------------------------------------------


class HelloMessage(WireModel):
    """First message on every ``/ws/live`` connection."""

    type: Literal["hello"]
    protocol: Literal["pyaerial.live"]
    version: int
    streams: list[StreamName]
    actions: list[ActionName]


class PingMessage(WireModel):
    type: Literal["ping"]
    timestamp: float


class FlightsMessage(WireModel):
    type: Literal["flights"]
    flights: list[FlightSummary]


class AlertsMessage(WireModel):
    type: Literal["alerts"]
    alerts: list[Alert]


class TelemetryMessage(WireModel):
    type: Literal["telemetry"]
    telemetry: list[TelemetryPoint]
    timestamp: float


class StatsMessage(WireModel):
    type: Literal["stats"]
    stats: ServerStats


PushedMessage = Annotated[
    HelloMessage
    | PingMessage
    | FlightsMessage
    | AlertsMessage
    | TelemetryMessage
    | StatsMessage,
    Field(discriminator="type"),
]


class LiveMessage(RootModel[PushedMessage]):
    """Any message the server sends on ``/ws/live`` other than ``response``."""


# -- RPC responses ---------------------------------------------------------------


class _Response(WireModel):
    type: Literal["response"]
    # The request's id if it is a string of at most 128 characters or an int.
    id: str | int | None


class ErrorResponse(_Response):
    """A failed request. ``error`` is for humans; branch on ``code``."""

    success: Literal[False]
    error: str
    code: ErrorCode


class _Success(_Response):
    success: Literal[True]


class SubscribeResponse(_Success):
    data: SubscribeResult


class FetchFlightsResponse(_Success):
    data: list[FlightSummary]


class FetchFlightResponse(_Success):
    data: FlightDetail


class FetchTelemetryResponse(_Success):
    data: list[TelemetryPoint]
    truncated: bool
    """The ``limit`` left points out (beside ``data``, which stays an array)."""


class FetchAlertsResponse(_Success):
    data: list[Alert]


class FetchStatsResponse(_Success):
    data: ServerStats


class FetchZonesResponse(_Success):
    data: ZonesData


class FetchConfigResponse(_Success):
    data: AppConfig


#: Successful response model per action.
RESPONSES: dict[str, type[_Success]] = {
    "subscribe": SubscribeResponse,
    "fetchFlights": FetchFlightsResponse,
    "fetchFlight": FetchFlightResponse,
    "fetchTelemetry": FetchTelemetryResponse,
    "fetchAlerts": FetchAlertsResponse,
    "fetchStats": FetchStatsResponse,
    "fetchZones": FetchZonesResponse,
    "fetchConfig": FetchConfigResponse,
}

#: Every model in the published schema (the rest are reached through these).
WIRE_MODELS: tuple[type[BaseModel], ...] = (
    LiveMessage,
    ErrorResponse,
    *RESPONSES.values(),
)

SCHEMA_TITLE = "PyAerial live WebSocket protocol"


def wire_json_schema() -> dict[str, Any]:
    """One JSON Schema document with every wire model under ``$defs``.

    Property titles (pydantic's "Flight Id" for ``flight_id``) are dropped as
    noise; model titles and docstrings are kept.
    """
    _, schema = models_json_schema(
        [(model, "validation") for model in WIRE_MODELS], title=SCHEMA_TITLE
    )
    for definition in schema["$defs"].values():
        for prop in definition.get("properties", {}).values():
            prop.pop("title", None)
    return schema
