"""Public webhook contract: the generic JSON body (``format: json``).

Schema ``pyaerial.alert/1``. Changes after 1.0 are additive only (new keys);
anything else needs ``pyaerial.alert/2``. ``tests/golden/webhook_*.json`` pins
the exact shape.

Every key is always present; "or null" marks values that can be ``null``.
Units are SI / WGS84 like the rest of PyAerial (see "Units" in CONFIGURATION.md).

=================  ==============  ======  ========================================
Field              Type            Unit    Meaning
=================  ==============  ======  ========================================
schema             string                  Always ``"pyaerial.alert/1"``
event              string                  ``"activate"``, ``"while_active"`` or
                                           ``"deactivate"``
alert_id           string                  Episode id; identical on the activate,
                                           every while_active and the deactivate of
                                           one episode (pair on this)
flight_id          string                  ``{icao}-{first_seen_unix}``; the flight
                                           the aircraft is tracked as now
icao               string                  24-bit address, 6 lowercase hex digits
callsign           string                  Flight id from ADS-B, else from the
                                           aircraft database; ``""`` if unknown
zone               string                  Zone name from the config
rule               string                  Rule name from the config
type               string                  Same as ``rule`` (kept from 0.x)
color              string                  ``#rrggbb`` (or ``#rgb``) rule/zone color
timestamp          number          s       Unix time the notification was raised
activated_at       number          s       Unix time the episode activated
deactivated_at     number or null  s       Unix time it ended; ``null`` unless
                                           ``event`` is ``"deactivate"``
ended_by           string or null          Why it ended (``deactivate`` only):
                                           ``"cleared"`` rule no longer holds,
                                           ``"lost"`` aircraft not heard for
                                           ``tracking.remember_planes``,
                                           ``"shutdown"`` engine stopping,
                                           ``"restart"`` left active by an engine
                                           that stopped uncleanly
eta                number or null  s       Time to reach the zone boundary along
                                           the projected track; 0 inside; ``null``
                                           if never (moving away, stationary) or
                                           unknown
reason             object                  ``{"zones": {zone: eta s or null},
                                           "rule": str, "hook": str (= event),
                                           "ended_by": str (deactivate only)}``;
                                           ETA to every configured zone
registration       string or null          Tail number (aircraft database)
manufacturer       string or null          Manufacturer code (aircraft database)
manufacturer_name  string or null          Manufacturer name (aircraft database)
model              string or null          Aircraft model (aircraft database)
aircraft_type      string or null          ICAO type designator, e.g. ``"A320"``
owner              string or null          Registered owner / operator
operator_callsign  string or null          Operator's radio callsign
country            string or null          Country of registration
photo_url          string or null          Aircraft photo (https)
telemetry          object                  Aircraft state when raised (below)
tracker_links      object                  ``{"flightaware", "adsbexchange",
                                           "radarbox"}`` -> https URL (empty object
                                           if ``icao`` is empty)
map_links          object                  ``{"google_maps", "openstreetmap"}`` ->
                                           https URL; empty object without position
=================  ==============  ======  ========================================

``telemetry``:

===============  ==============  ======  ==========================================
Field            Type            Unit    Meaning
===============  ==============  ======  ==========================================
latitude         number or null  deg     WGS84 latitude (dead-reckoned during gaps)
longitude        number or null  deg     WGS84 longitude, -180..180
altitude         number or null  m       Barometric altitude, else GNSS height
altitude_source  string or null          ``"baro"`` or ``"gnss"`` (geometric height
                                         above the WGS84 ellipsoid)
speed            number or null  km/h    Ground speed
heading          number or null  deg     Track, clockwise from true north
vertical_speed   number or null  m/s     Climb (+) / descent (-) rate
===============  ==============  ======  ==========================================
"""

from __future__ import annotations

import math
from typing import Literal, TypedDict

SCHEMA = "pyaerial.alert/1"
EVENTS = ("activate", "while_active", "deactivate")

Event = Literal["activate", "while_active", "deactivate"]

#: Aircraft database fields; always present in the body, null when unknown.
AIRCRAFT_FIELDS = (
    "registration",
    "manufacturer",
    "manufacturer_name",
    "model",
    "aircraft_type",
    "owner",
    "operator_callsign",
    "country",
    "photo_url",
)
TELEMETRY_FIELDS = (
    "latitude",
    "longitude",
    "altitude",
    "altitude_source",
    "speed",
    "heading",
    "vertical_speed",
)


class AlertTelemetry(TypedDict):
    latitude: float | None
    longitude: float | None
    altitude: float | None
    altitude_source: Literal["baro", "gnss"] | None
    speed: float | None
    heading: float | None
    vertical_speed: float | None


class AlertBody(TypedDict):
    schema: str
    event: Event
    alert_id: str
    flight_id: str
    icao: str
    callsign: str
    zone: str
    rule: str
    type: str
    color: str
    timestamp: float
    activated_at: float
    deactivated_at: float | None
    ended_by: str | None
    eta: float | None
    reason: dict
    registration: str | None
    manufacturer: str | None
    manufacturer_name: str | None
    model: str | None
    aircraft_type: str | None
    owner: str | None
    operator_callsign: str | None
    country: str | None
    photo_url: str | None
    telemetry: AlertTelemetry
    tracker_links: dict[str, str]
    map_links: dict[str, str]


def event_of(meta: dict) -> str:
    """The event (``reason.hook``) of an alerter ``meta`` dict, or ``""``."""
    reason = meta.get("reason")
    hook = reason.get("hook") if isinstance(reason, dict) else None
    return str(hook) if hook else ""


def ended_by_of(meta: dict) -> str | None:
    reason = meta.get("reason")
    value = reason.get("ended_by") if isinstance(reason, dict) else None
    return str(value) if value else None


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def build_alert_body(
    meta: dict,
    payload: dict,
    tracker_links: dict[str, str],
    map_links: dict[str, str],
) -> AlertBody:
    """The ``pyaerial.alert/1`` body from the alert engine's meta and payload."""
    rule = str(meta.get("type") or "")
    telemetry: AlertTelemetry = {  # type: ignore[assignment]
        field: (
            payload.get(field)
            if field == "altitude_source"
            else _finite(payload.get(field))
        )
        for field in TELEMETRY_FIELDS
    }
    body: AlertBody = {
        "schema": SCHEMA,
        "event": event_of(meta),  # type: ignore[typeddict-item]
        "alert_id": str(meta.get("alert_id") or ""),
        "flight_id": str(meta.get("flight_id") or ""),
        "icao": str(meta.get("icao") or "").lower(),
        "callsign": str(meta.get("callsign") or ""),
        "zone": str(meta.get("zone") or ""),
        "rule": rule,
        "type": rule,
        "color": str(meta.get("color") or ""),
        "timestamp": _finite(meta.get("timestamp")),  # type: ignore[typeddict-item]
        "activated_at": _finite(meta.get("activated_at")),  # type: ignore[typeddict-item]
        "deactivated_at": _finite(meta.get("deactivated_at")),
        "ended_by": ended_by_of(meta),
        "eta": _finite(meta.get("eta")),
        "reason": meta.get("reason") if isinstance(meta.get("reason"), dict) else {},
        **{field: (meta.get(field) or None) for field in AIRCRAFT_FIELDS},  # type: ignore[typeddict-item]
        "telemetry": telemetry,
        "tracker_links": dict(tracker_links),
        "map_links": dict(map_links),
    }
    return body
