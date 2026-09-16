"""Live WebSocket protocol constants and discovery document."""

from __future__ import annotations

from typing import Any

WS_PROTOCOL = "pyaerial.live"
WS_VERSION = 1
WS_PATH = "/ws/live"
WS_ALIASES = ("/ws",)
WS_STREAMS = ("flights", "alerts", "telemetry", "stats")
WS_ACTIONS = (
    "subscribe",
    "fetchFlights",
    "fetchFlight",
    "fetchTelemetry",
    "fetchAlerts",
    "fetchStats",
    "fetchZones",
    "fetchConfig",
)


def websocket_hello() -> dict[str, Any]:
    return {
        "type": "hello",
        "protocol": WS_PROTOCOL,
        "version": WS_VERSION,
        "streams": list(WS_STREAMS),
        "actions": list(WS_ACTIONS),
    }


def websocket_api_spec() -> dict[str, Any]:
    return {
        "protocol": WS_PROTOCOL,
        "version": WS_VERSION,
        "websocket": WS_PATH,
        "aliases": list(WS_ALIASES),
        "auth": {
            "query": "token",
            "header": "x-pyaerial-token",
            "note": "Required only when web.token is set.",
        },
        "connect": (
            "Open the socket, read the hello + snapshot (flights, alerts, stats), "
            "then either listen for pushed streams or send type=request messages."
        ),
        "streams": {
            "flights": "Full live flight list whenever positions or alerts change.",
            "alerts": "Live alert episodes for currently tracked flights.",
            "telemetry": "New track points since the client last received telemetry.",
            "stats": (
                "live_flights / active_alerts / retained_flights / historical_alerts, "
                "plus redis / mongo booleans and engine_seen_at (unix seconds, or null "
                "if the tracking engine is not writing a heartbeat)."
            ),
        },
        "client_request": {
            "type": "request",
            "id": "opaque-id",
            "action": "fetchFlights | fetchFlight | fetchTelemetry | fetchAlerts | fetchStats | fetchZones | fetchConfig | subscribe",
            "params": {},
        },
        "actions": {
            "subscribe": {
                "params": {"streams": list(WS_STREAMS)},
                "notes": "Limit which streams this connection receives. Omit or pass [] for all.",
            },
            "fetchFlights": {
                "params": {
                    "view": "live | history",
                    "skip": 0,
                    "limit": 50,
                    "q": None,
                    "since": None,
                    "until": None,
                },
                "notes": "History q matches ICAO, callsign, or flight id. since/until are unix seconds on end_time.",
            },
            "fetchFlight": {"params": {"flightId": "required", "view": "live | history"}},
            "fetchTelemetry": {
                "params": {"flightId": "required", "view": "live | history", "since": 0}
            },
            "fetchAlerts": {
                "params": {
                    "view": "live | history",
                    "skip": 0,
                    "limit": 0,
                    "q": None,
                    "since": 0,
                    "until": None,
                    "flightId": None,
                    "rule": None,
                    "active_only": None,
                }
            },
            "fetchStats": {"params": {}},
            "fetchZones": {"params": {}},
            "fetchConfig": {"params": {}},
        },
        "server_messages": ["hello", "flights", "alerts", "telemetry", "stats", "ping", "response"],
    }
