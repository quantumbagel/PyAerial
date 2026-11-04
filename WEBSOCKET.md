# WebSocket and Streaming API

The `pyaerial web` service exposes real-time aircraft feeds, historical queries, Prometheus metrics, and reconstructed binary Mode S data over WebSockets and TCP.

## Endpoints

| Endpoint                                      | Format          | Description                                                                                                       |
| --------------------------------------------- | --------------- | ----------------------------------------------------------------------------------------------------------------- |
| `ws://<host>:<port>/ws/live` (or alias `/ws`) | JSON            | Real-time push streams and bidirectional RPC actions adhering to protocol `pyaerial.live` version 1               |
| `ws://<host>:<port>/ws/raw`                   | JSON            | Stream of individual Mode S frames with signal quality metrics for debugging and raw visualization                |
| `ws://<host>:<port>/ws/beast`                 | Binary          | Cleaned and merged frames packaged into dump1090 Beast binary format                                              |
| `tcp://<beast_host>:<beast_port>`             | Binary          | Outgoing TCP socket serving the same Beast binary stream as `/ws/beast` (defaults to port 30105)                  |
| `GET /api`                                    | JSON            | Discovery document advertising service version and active connection limits                                       |
| `GET /health`                                 | JSON            | Basic liveness probe returning `{"status": "ok"}`                                                                 |
| `GET /ready`                                  | JSON            | Readiness check reporting status for Redis, the archive, and the engine; returns HTTP 503 if Redis is unreachable |
| `GET /metrics`                                | Prometheus text | Operational metrics in Prometheus 0.0.4 format; can be disabled with `web.metrics: false`                         |
| `/docs`, `/redoc`, `/openapi.json`            | HTML / JSON     | Interactive API documentation, available only when `web.enable_docs: true`                                        |

Specialized feeds (`/ws/raw`, `/ws/beast`, and Beast TCP) transmit data without initial handshake greetings, do not accept RPC request payloads, and do not emit keepalive ping messages.

## Prometheus metrics

The `/metrics` endpoint is designed for direct scraping by Prometheus collectors (for example at `http://127.0.0.1:10090/metrics`). Scraping performs an internal health check alongside a single Redis query to inspect the engine's latest heartbeat.

Web portal metrics include:

- `pyaerial_build_info{version}` (constant gauge set to 1)
- `pyaerial_redis_up` and `pyaerial_archive_up` indicating backing storage reachability
- `pyaerial_websocket_connections{endpoint}` tracking active connections across `/ws/live`, `/ws/raw`, and `/ws/beast`
- `pyaerial_beast_tcp_clients` counting attached Beast TCP subscribers
- `pyaerial_websocket_queued_bytes` measuring outbound buffer pressure
- `pyaerial_websocket_rate_limited_total` recording RPC requests rejected due to rate limits
- Client drop counters labeled by reason, such as `pyaerial_websocket_clients_dropped_total{reason}` (`client_too_slow`, `server_budget`, or `feed_behind`) and `pyaerial_beast_tcp_clients_dropped_total{reason}` (`feed_behind` or `not_reading`)

Engine metrics derived from the live heartbeat include:

- `pyaerial_engine_up`, which reports 1 whenever the engine heartbeat was updated within the last 10 seconds
- When the engine is healthy, additional gauges report `pyaerial_engine_start_time_seconds`, `pyaerial_engine_planes_tracked`, `pyaerial_engine_history_up`, and `pyaerial_engine_history_pending`
- Cumulative engine counters that reset whenever the engine restarts, including `pyaerial_engine_frames_received_total`, `_frames_dropped_total`, `_messages_processed_total`, `_pending_evicted_total`, `_receiver_restarts_total{receiver}`, and `_stage_errors_total{stage}`

Metric labels include configured receiver names rather than sensitive network addresses or credentials. Standard host filtering rules apply to the metrics endpoint, and requests originating from browsers must satisfy origin checks.

## Allowed origins

Browsers connecting to WebSocket routes must supply an accepted `Origin` header, or the connection will be rejected with status code `1008` (Policy Violation). Non-browser clients that omit the `Origin` header entirely are accepted by default. Allowed origins include:

- Same-origin connections where the `Origin` host and port match the request `Host` header, provided the portal is reached via an IP address, `localhost`, or a hostname listed in `web.allowed_hosts`.
- Specific URLs configured in `web.origins` (note that scheme and port must match exactly, meaning `https://portal.example` does not cover `http://portal.example` or alternate ports); setting `["*"]` allows any origin.
- Local development origins such as `localhost`, `127.0.0.1`, or `[::1]` when `web.allow_localhost_origins: true` is enabled.

## Connection limits and backpressure

| Limit                                 | Default config setting                                                             | Handling when exceeded                                                                                                          |
| ------------------------------------- | ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| Total WebSocket connections           | 256 (`web.ws_max_connections`)                                                     | Closes the connection with status `1013` (Try Again Later)                                                                      |
| Connections per client IP             | 32 (`web.ws_max_connections_per_ip`)                                               | Closes the connection with status `1013`                                                                                        |
| In-flight RPC requests per connection | 2 (`web.ws_max_inflight`)                                                          | Server pauses reading from the socket until an existing response has been sent                                                  |
| RPC rate per connection               | 50/sec with burst of 100 (`web.ws_rate_limit`, `web.ws_rate_burst`)                | Delays processing; requests that would wait longer than 1 second receive error code `rate_limited`                              |
| RPC rate per client IP                | 200/sec with burst of 400 (`web.ws_rate_limit_per_ip`, `web.ws_rate_burst_per_ip`) | Same rate-limiting behavior applied across all connections from that IP                                                         |
| Inbound frame size                    | 64 KiB (fixed limit)                                                               | Closes the connection with status `1009` (Message Too Big)                                                                      |
| Outbound buffer per client            | 64 messages or 8 MB (fixed limit)                                                  | Skips or coalesces older stream messages; clients whose unread RPC responses exceed the limit are disconnected with code `1013` |
| Combined outbound buffer              | 256 MB (fixed limit)                                                               | Disconnects the slowest client holding the largest backlog with code `1013`                                                     |
| Slow raw frame reader                 | 10 seconds behind (fixed limit)                                                    | Disconnects `/ws/raw` or `/ws/beast` clients falling behind with code `1013`                                                    |
| Beast TCP subscribers                 | 16 total, up to 4 per IP (`web.beast_max_clients`, `web.beast_max_clients_per_ip`) | Additional incoming connections are rejected; subscribers that stop reading or fall 10 seconds behind are closed                |

If you run PyAerial behind a reverse proxy, configure the proxy's IP address or CIDR range in `web.trusted_proxies` so the server can extract client addresses from `X-Forwarded-For`. Otherwise, all clients will share the proxy's IP address and contend for the same per-IP limits.

## Live feed (`/ws/live`)

When a client connects to `/ws/live`, the server immediately sends a `hello` handshake message, followed by initial snapshots of the subscribed `flights`, `alerts`, and `stats` streams. After the initial snapshot, it pushes incremental updates whenever state changes, along with a keepalive ping payload (`{"type": "ping", "timestamp": ...}`) every 15 seconds.

```json
{
  "type": "hello",
  "protocol": "pyaerial.live",
  "version": 1,
  "streams": ["flights", "alerts", "telemetry", "stats"],
  "actions": [
    "subscribe",
    "fetchFlights",
    "fetchFlight",
    "fetchTelemetry",
    "fetchAlerts",
    "fetchStats",
    "fetchZones",
    "fetchConfig"
  ]
}
```

Message models and validation schemas are defined using Pydantic in `src/pyaerial/api/models.py` and exported as JSON Schema in [`web/src/api/schema.json`](web/src/api/schema.json). Clients should safely ignore any unknown fields or message types, as the major version only changes when fields are removed or fundamentally altered.

By default, all four streams are activated upon connection. You can customize your subscriptions either by adding query parameters like `/ws/live?streams=flights,alerts` or by sending a `subscribe` RPC request. If Redis becomes temporarily unreachable, the server halts updates on `flights` and `alerts` and sets `stats.redis` to `false`.

| Stream message | Payload contents                                                                                 | Delivery timing                                                                         |
| -------------- | ------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------- |
| `flights`      | Summaries of all currently active flights under the `flights` key                                | Pushed whenever aircraft positions, metadata, active alerts, or integrity scores update |
| `alerts`       | Array of active alert episodes along with recent inactive episodes up to `max(50, active count)` | Pushed on changes; send `fetchAlerts` if you need the exhaustive historical list        |
| `telemetry`    | Recent track points under `telemetry` arriving after client connection                           | Pushed as fresh points arrive; send `fetchTelemetry` to retrieve past points            |
| `stats`        | General system counters including `live_flights`, `active_alerts`, `redis`, and engine health    | Pushed on changes                                                                       |

A typical flight summary object shared across live and historical views looks like this:

```json
{
  "flight_id": "a1b2c3-1721832000",
  "icao": "a1b2c3",
  "callsign": "AAL123",
  "model": "Boeing 737-800",
  "owner": "American Airlines",
  "country": "United States",
  "aircraft_type": "L2J",
  "registration": "N123AA",
  "start_time": 1721832000.0,
  "end_time": 1721832015.2,
  "timestamp": 1721832015.2,
  "latitude": 35.7288,
  "longitude": -78.6954,
  "altitude": 1250.0,
  "speed": 420.5,
  "heading": 185.0,
  "is_live": true,
  "status": "live",
  "retained": false,
  "integrity": {
    "score": 1.0,
    "status": "ok",
    "checks": {
      "radio_horizon": {
        "status": "pass",
        "detail": "main: 42 km, radio horizon 441 km",
        "value": 0.1
      }
    }
  },
  "active_alerts": [
    {
      "alert_id": "a1b2c3-1721832000:airport_approach:low_altitude_warning",
      "zone": "airport_approach",
      "rule": "low_altitude_warning",
      "activated_at": 1721832010.0,
      "eta": 45.0
    }
  ],
  "alert_stats": {
    "episode_count": 1,
    "total_seconds": 5,
    "active_count": 1
  }
}
```

Units follow the standards documented in [CONFIGURATION.md](CONFIGURATION.md): altitudes in meters, speeds in kilometers per hour, headings in degrees, and timestamps in Unix epoch seconds. Missing metadata values are set to `null` rather than arbitrary placeholders like `"UNKNOWN"`. The `integrity` object remains `null` until the engine gathers enough frames to calculate a score, after which it includes `status`, `score`, and individual check results.

The flight `status` field is set to either `live` or `completed`. If an engine crash or unexpected shutdown prevented a flight from being finalized cleanly, the record in history includes `interrupted: true` with its trajectory ending at the last recorded checkpoint.

Alert episodes include the episode identifier (`alert_id`), flight ID, ICAO address, callsign, zone name, rule name, activation timestamps, and position details. Once an alert ends, `ended_by` records why it closed (`cleared`, `lost`, `shutdown`, or `restart`), remaining `null` while the alert is active.

Telemetry samples record latitude, longitude, barometric altitude, ground speed, heading, and the receive timestamp. If geometric altitude was decoded from ADS-B TC 20–22 messages, `gnss_height` is included as well. Samples also record the list of receivers that picked up the message along with the integrity verdict in effect when the frame arrived.

To determine whether the tracking engine is actively running, inspect `stats.engine_live` rather than comparing `engine_seen_at` against local browser clocks.

## RPC actions

Clients can issue request-response commands directly over the `/ws/live` connection:

```json
{
  "type": "request",
  "id": "req-42",
  "action": "fetchFlight",
  "params": {
    "flightId": "a1b2c3-1721832000",
    "view": "live"
  }
}
```

The server responds by echoing your request `id` along with either a successful result or an error block:

```json
{"type": "response", "id": "req-42", "success": true, "data": {"flight_id": "a1b2c3-1721832000", "photo_url": "https://images.planespotters.net/photo/..."}}
{"type": "response", "id": "req-42", "success": false, "error": "not found", "code": "not_found"}
```

Clients should inspect `code` when handling errors:

| Error code            | Meaning                                                                                                                            |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `bad_request`         | The request payload contained malformed JSON, unparseable binary data, an unrecognized action name, or missing required parameters |
| `not_found`           | The requested flight identifier could not be located                                                                               |
| `rate_limited`        | The request rate exceeded configured thresholds and could not be buffered; try again later                                         |
| `history_unavailable` | The SQLite archive is enabled in the configuration but cannot currently be opened or read                                          |
| `live_unavailable`    | Redis is disconnected or offline                                                                                                   |
| `internal_error`      | An unexpected error occurred while processing the request                                                                          |

The `view` parameter accepts `"live"` (default) or `"history"`. Available actions include:

- `subscribe`: Updates the stream subscription list for the current connection, accepting an array of stream names under `streams` (or all streams if omitted). Newly subscribed `flights`, `alerts`, and `stats` streams immediately receive current snapshot messages.
- `fetchFlights`: Retrieves flight summaries. In live mode, it returns all active flights. In history mode, it supports pagination via `skip` (0–10000) and `limit` (1–200, defaulting to 50), along with substring filtering with `q` and timestamp bounds via `since` and `until`.
- `fetchFlight`: Fetches detailed records for a specific `flightId`, including operator data, airframe specifications, and Planespotters photo links.
- `fetchTelemetry`: Returns ordered trajectory points for a flight. By default, it returns up to 5000 points (`limit` can be set up to 20000) along with a `truncated` boolean indicator. Providing a `since` timestamp allows clients to page forward chronologically.
- `fetchAlerts`: Queries alert episodes. Live queries default to currently active alerts, while historical queries support pagination, rule name filtering, and time ranges.
- `fetchStats`: Returns the current system statistics payload, matching what the `stats` stream emits.
- `fetchZones`: Returns the configured station reference coordinates, geofence polygons, and map alert colors.
- `fetchConfig`: Returns safe public configuration details, such as station coordinates, receiver names and positions, and Beast feed ports, while omitting private receiver credentials, hostnames, and ports.

## Raw frames (`/ws/raw`)

The `/ws/raw` WebSocket pushes batches of decoded Mode S frames:

```json
{
  "type": "raw",
  "frames": [
    {
      "hex": "8d406b902015a678d4d220aa4bda",
      "timestamp": 1730000000.125,
      "receiver": "strong",
      "receivers": ["strong", "weak"],
      "rssi": -8.0,
      "clock": 12,
      "df": 17,
      "icao": "406b90"
    }
  ]
}
```

The `timestamp` reflects when the engine received the frame. The `receiver` property names the receiver with the strongest signal, while `receivers` lists all stations that heard the transmission in order of signal strength. Signal levels (`rssi`) are expressed in dBFS.

## Beast binary feed (`/ws/beast` and TCP)

Instead of simply retransmitting dump1090's raw TCP output, PyAerial merges incoming frames across all stations, selects the cleanest frame that passes CRC checks, removes duplicate messages received within `tracking.duplicate_packet_merging` seconds, and encodes the stream into standard dump1090 Beast binary format:

```
<0x1a> <type> <6-byte 12 MHz clock> <1-byte signal> <Mode S payload>
```

Frame format details:

- Frame type bytes use `0x31` for 2-byte Mode A/C, `0x32` for 7-byte Mode S short messages, and `0x33` for 14-byte Mode S long messages. Any `0x1a` byte appearing in the payload or header is escaped by doubling it.
- Clock timestamps are synthesized from the engine's monotonic receive time using `int(timestamp * 12e6) mod 2^48`, giving all merged receiver sources a shared, synchronized time base.
- Signal levels represent the signal byte standard used by dump1090-fa (`sqrt(signalLevel) * 255`), which corresponds to `20 * log10(byte / 255)` dBFS.

Feeders such as OpenSky Network can consume this stream directly by pointing to PyAerial's Beast TCP port (configured via `web.beast_host` and `web.beast_port` in [CONFIGURATION.md](CONFIGURATION.md)):

```yaml
opensky:
  image: ghcr.io/sdr-enthusiasts/docker-opensky-network:latest
  environment:
    - BEASTHOST=pyaerial_web
    - BEASTPORT=30105
    - LAT=35.727488
    - LONG=-78.695942
    - ALT=120
    - OPENSKY_USERNAME=youruser
```

## Python client example

This example demonstrates subscribing to live flights and issuing a historical query using the `websockets` library:

```python
import asyncio
import json
import websockets

async def main():
    uri = "ws://127.0.0.1:10090/ws/live?streams=flights,alerts"
    async with websockets.connect(uri) as ws:
        greeting = json.loads(await ws.recv())
        assert greeting["type"] == "hello"

        # Request recent historical flights
        request = {
            "type": "request",
            "id": "hist-req",
            "action": "fetchFlights",
            "params": {"view": "history", "limit": 10}
        }
        await ws.send(json.dumps(request))

        async for raw_message in ws:
            msg = json.loads(raw_message)
            msg_type = msg.get("type")

            if msg_type == "flights":
                for flight in msg.get("flights", []):
                    print(f"Live: {flight['icao']} {flight.get('callsign')} at {flight.get('altitude')}m")
            elif msg_type == "alerts":
                for alert in msg.get("alerts", []):
                    if alert.get("active"):
                        print(f"Alert in zone {alert['zone']}: {alert['rule']} on {alert['icao']}")
            elif msg_type == "response" and msg.get("id") == "hist-req":
                flights = msg.get("data", [])
                print("History results:", [f["flight_id"] for f in flights])

asyncio.run(main())
```
