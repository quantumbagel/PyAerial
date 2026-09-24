"""Live WebSocket protocol constants and discovery document."""

from __future__ import annotations

from typing import Any

WS_PROTOCOL = "pyaerial.live"
WS_VERSION = 1
WS_PATH = "/ws/live"
WS_BEAST_PATH = "/ws/beast"
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


def websocket_api_spec(
    *,
    beast_host: str | None = None,
    beast_port: int | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "protocol": WS_PROTOCOL,
        "version": WS_VERSION,
        "websocket": WS_PATH,
        "beast_websocket": WS_BEAST_PATH,
        "aliases": list(WS_ALIASES),
        "connect": (
            "Open /ws/live, read the hello + snapshot (flights, alerts, stats), "
            "then either listen for pushed streams or send type=request messages. "
            "Optional /ws/live?streams=flights,alerts limits the initial set. "
            "Corrected Mode S frames are re-encoded as dump1090 Beast binary on "
            "/ws/beast (binary WebSocket, no JSON handshake) and, when "
            "web.beast_port is set, a TCP listener compatible with OpenSky "
            "Network's feeder (BEASTHOST/BEASTPORT, default dump1090 port 30005). "
            "Beast is not a /ws/live subscribe stream."
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
        },
        "beast": {
            "websocket": WS_BEAST_PATH,
            "tcp_host": beast_host,
            "tcp_port": beast_port,
            "format": "dump1090 Beast binary (0x1a type + 6-byte 12 MHz clock + "
            "signal byte + Mode S payload, 0x1a escaped). Frames are merged "
            "across receivers (stronger RSSI, Hamming vote on near-copies) "
            "before encoding.",
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
                    "Beast frames are not a live stream; connect to /ws/beast or the "
                    "Beast TCP port instead."
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
            "ping",
            "response",
        ],
    }
    return spec
