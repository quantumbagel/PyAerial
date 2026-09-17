"""Live WebSocket protocol constants and discovery document."""

from __future__ import annotations

from typing import Any

WS_PROTOCOL = "pyaerial.live"
WS_RAW_PROTOCOL = "pyaerial.raw"
WS_VERSION = 1
WS_PATH = "/ws/live"
WS_RAW_PATH = "/ws/raw"
WS_ALIASES = ("/ws",)
WS_STREAMS = ("flights", "alerts", "telemetry", "stats")
WS_RAW_STREAMS = ("raw",)
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


def available_streams() -> list[str]:
    return list(WS_STREAMS)


def websocket_hello() -> dict[str, Any]:
    return {
        "type": "hello",
        "protocol": WS_PROTOCOL,
        "version": WS_VERSION,
        "streams": available_streams(),
        "actions": list(WS_ACTIONS),
    }


def websocket_raw_hello() -> dict[str, Any]:
    return {
        "type": "hello",
        "protocol": WS_RAW_PROTOCOL,
        "version": WS_VERSION,
        "streams": list(WS_RAW_STREAMS),
        "actions": [],
    }


def websocket_api_spec() -> dict[str, Any]:
    return {
        "protocol": WS_PROTOCOL,
        "raw_protocol": WS_RAW_PROTOCOL,
        "version": WS_VERSION,
        "websocket": WS_PATH,
        "raw_websocket": WS_RAW_PATH,
        "aliases": list(WS_ALIASES),
        "auth": {
            "query": "token",
            "header": "x-pyaerial-token",
            "note": "Required only when web.token is set.",
        },
        "connect": (
            "Open /ws/live, read the hello + snapshot (flights, alerts, stats), "
            "then either listen for pushed streams or send type=request messages. "
            "Raw dump1090 frames are a separate socket at /ws/raw only (not on "
            "/ws/live, and not via subscribe or ?streams=). /ws/raw speaks "
            "pyaerial.raw and does not accept live RPC actions."
        ),
        "streams": {
            "flights": "Full live flight list whenever positions or alerts change.",
            "alerts": (
                "Live alert episodes for currently tracked flights. Pushed snapshots "
                "are the 50 newest tracked episodes; fetchAlerts is the full list."
            ),
            "telemetry": "New track points since the client last received telemetry.",
            "stats": (
                "live_flights / active_alerts / retained_flights / historical_alerts, "
                "plus redis / history booleans and engine_seen_at (unix seconds, or null "
                "if the tracking engine is not writing a heartbeat)."
            ),
            "raw": (
                "dump1090 / receiver frames as they arrive. Only on /ws/raw, never on "
                "/ws/live. Each batch is type=raw with messages[]. hex, timestamp "
                "(unix seconds, engine receive time), receiver; df / icao when "
                "decodable; rssi (dBFS) and clock (12 MHz ticks, 48-bit integer; "
                "1 tick = 1/12 000 000 s) when the receiver uses Beast (dump1090 "
                "port 30005) or timestamped AVR (@CLOCKHEX). On connect the server "
                "sends type=antenna with home lat/lon and configured receivers."
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
                "params": {"streams": available_streams()},
                "notes": (
                    "Limit which /ws/live streams this connection receives. Omit or "
                    "pass [] for the default set (flights, alerts, telemetry, stats). "
                    "'raw' is not a live stream; connect to /ws/raw instead."
                ),
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
                "notes": (
                    "History q matches ICAO, callsign, or flight id. since/until are "
                    "unix seconds on end_time. History limit is capped at 200."
                ),
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
        "server_messages": [
            "hello",
            "flights",
            "alerts",
            "telemetry",
            "stats",
            "raw",
            "antenna",
            "ping",
            "response",
        ],
    }
