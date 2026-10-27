# WebSocket API

PyAerial exposes real-time aircraft tracking, spatial alert notifications, historical telemetry queries, and dump1090-compatible Beast Mode S frames through the live portal process.

**Endpoints and subprotocols**

| Endpoint | Subprotocol | Push streams | RPC actions | Description |
|----------|-------------|--------------|-------------|-------------|
| `ws://<host>:<port>/ws/live` | `pyaerial.live` | `flights`, `alerts`, `telemetry`, `stats`, `ping` | Supported | Main real-time application feed (alias `/ws`) |
| `ws://<host>:<port>/ws/beast` | binary Beast | Mode S frames | Not supported | dump1090 Beast binary (no JSON handshake) |
| `tcp://<host>:<beast_port>` | Beast TCP | Mode S frames | Not supported | Same bytes as `/ws/beast`; default port `30005` for OpenSky Network |
| `http://<host>:<port>/api` | HTTP (JSON) | None | None | Machine-readable API discovery specification |

**Origin validation**

Browser connections to `/ws/live` and `/ws/beast` must present an allowed `Origin`:

- **Same origin** (the portal's own page): the `Origin` host and port equal the request `Host` header. Always allowed.
- **`web.origins`**: exact `scheme://host[:port]` matches (`https://portal.example` does not match `http://portal.example` or `https://portal.example:8443`). Default `[]`; `["*"]` accepts any origin.
- **`web.allow_localhost_origins: true`**: any `http(s)://localhost`, `127.0.0.1` or `[::1]` origin on any port (Vite dev servers). Off by default.

Native clients that omit `Origin` connect unconditionally. Unauthorized origins are rejected with close code `1008` (`origin not allowed`). Same-origin (Origin matching the `Host` header) is accepted automatically when the portal is reached by IP address or `localhost`. If you serve it under a hostname (`pi.local`, a reverse-proxy domain), list that name in `web.allowed_hosts`; unlisted hostnames are refused so DNS-rebinding pages cannot pose as same-origin.

**Connection and request limits**

| Limit | Default | Config key | Behaviour when exceeded |
|-------|---------|------------|-------------------------|
| WebSocket connections (`/ws/live` + `/ws/beast`) | 256 | `web.ws_max_connections` | Close `1013` (`server connection limit reached`) |
| Connections per client IP | 32 | `web.ws_max_connections_per_ip` | Close `1013` (`per-client connection limit reached`) |
| In-flight RPC requests per connection | 2 | `web.ws_max_inflight` | Server stops reading the socket until one finishes (back-pressure, no error) |
| Request rate per connection | 50/s, burst 100 | `web.ws_rate_limit`, `web.ws_rate_burst` | Requests are delayed to the rate; one that would wait over 1 s gets `code: "rate_limited"` |
| Inbound frame size | 64 KiB | fixed | Close `1009` (message too big) |
| Beast TCP feeders | 16 | `web.beast_max_clients` | Extra connections are closed immediately |

`skip` is capped at 10000 and history `fetchTelemetry` at 20000 points. A client that stops reading its socket until its outbox fills with unread RPC responses is disconnected with close code `1013`. RPC handlers run on a dedicated pool of `web.ws_request_workers` threads (default 4). The active values are listed under `limits` in `GET /api`.

**Live stream interface (`/ws/live`)**

Clients can restrict which push events they receive by supplying a comma-separated query parameter during connection (`/ws/live?streams=flights,alerts`) or by issuing a `subscribe` RPC action after connection. If omitted, the server defaults to delivering all four live streams (`flights`, `alerts`, `telemetry`, `stats`).

Upon connecting, the server executes a deterministic handshake sequence:

1. Transmits a `hello` handshake message containing protocol versioning, active push streams, and callable actions.
2. Transmits immediate snapshot messages for all subscribed streams (`flights`, `alerts`, and `stats`).
3. Transitions to pushing incremental state updates as aircraft move or alert episodes transition.
4. Emits a keepalive `ping` message every 15 seconds to prevent network intermediate timeouts.

**Pushed message schemas**

Server handshake message:

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

Aircraft flight list broadcast (`type: "flights"`), pushed whenever positions, velocities, or alert states update. Live and history summaries share the metadata fields `callsign`, `model`, `owner`, `country`, `aircraft_type` and `registration`; unknown values are `null` (placeholders such as `"Unknown Model"`, `"UNKNOWN"` or `""` are never sent). `integrity` is the spoofing assessment described in [CONFIGURATION.md](CONFIGURATION.md#configuration), or `null` before the engine has scored the aircraft. Every `active_alerts[].alert_id` is non-empty (`<flight_id>:<zone>:<rule>` when the store has no id):

```json
{
  "type": "flights",
  "flights": [
    {
      "flight_id": "a1b2c3-1721832000",
      "icao": "a1b2c3",
      "callsign": "AAL123",
      "model": "Boeing 737-800",
      "owner": "American Airlines",
      "country": "United States",
      "aircraft_type": "L2J",
      "registration": "N123AA",
      "latitude": 35.7288,
      "longitude": -78.6954,
      "altitude": 1250.0,
      "speed": 420.5,
      "heading": 185.0,
      "is_live": true,
      "status": "live",
      "retained": false,
      "timestamp": 1721832015.2,
      "integrity": {
        "score": 1.0,
        "status": "ok",
        "checks": {
          "radio_horizon": {"status": "pass", "detail": "main: 42 km, radio horizon 441 km", "value": 0.1}
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
  ]
}
```

Active alerts broadcast (`type: "alerts"`) for currently tracked flights: every active episode plus the newest inactive ones, `max(50, active count)` episodes in total when that many exist, newest first. Use `fetchAlerts` for the full list:

```json
{
  "type": "alerts",
  "alerts": [
    {
      "alert_id": "a1b2c3-1721832000:airport_approach:low_altitude_warning",
      "flight_id": "a1b2c3-1721832000",
      "icao": "a1b2c3",
      "callsign": "AAL123",
      "zone": "airport_approach",
      "rule": "low_altitude_warning",
      "active": true,
      "activated_at": 1721832010.0,
      "deactivated_at": null,
      "eta": 45.0,
      "altitude": 1250.0,
      "latitude": 35.7288,
      "longitude": -78.6954
    }
  ]
}
```

Incremental telemetry points (`type: "telemetry"`), delivering only points newer than the last one this client received for the same flight (a late write for one flight is not hidden by another flight's newer point). Points also carry, when known, `receivers` (receivers that heard the position, strongest copy first) and the integrity verdict in effect when the point arrived (`integrity_score`, `integrity_status`, `integrity_flags`); `fetchTelemetry` returns the same fields for live and history views. See the research archive section of [CONFIGURATION.md](CONFIGURATION.md):

```json
{
  "type": "telemetry",
  "timestamp": 1721832015.5,
  "telemetry": [
    {
      "flight_id": "a1b2c3-1721832000",
      "icao": "a1b2c3",
      "latitude": 35.7288,
      "longitude": -78.6954,
      "altitude": 1250.0,
      "speed": 420.5,
      "heading": 185.0,
      "timestamp": 1721832015.2,
      "receivers": ["roof", "mast"],
      "integrity_score": 0.96,
      "integrity_status": "ok",
      "integrity_flags": []
    }
  ]
}
```

System statistics broadcast (`type: "stats"`):

```json
{
  "type": "stats",
  "stats": {
    "live_flights": 14,
    "active_alerts": 2,
    "retained_flights": 1280,
    "historical_alerts": 412,
    "redis": true,
    "history": true,
    "engine_seen_at": 1721832015.0,
    "engine_live": true
  }
}
```

`engine_live` is computed on the server from the engine heartbeat (fresh within 10 s). Prefer it over comparing `engine_seen_at` with the browser clock.

**RPC request and response protocol**

Clients issue RPC commands over `/ws/live` by sending JSON objects with `type: "request"`. The server echoes the client's opaque `id` string in the response.

Client request template:

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

Successful server reply:

```json
{
  "type": "response",
  "id": "req-42",
  "success": true,
  "data": {
    "flight_id": "a1b2c3-1721832000",
    "icao": "a1b2c3",
    "callsign": "AAL123",
    "registration": "N123AA",
    "photo_url": "https://images.planespotters.net/photo/...",
    "photo_photographer": "Jane Doe",
    "photo_link": "https://www.planespotters.net/photo/..."
  }
}
```

Failed server reply:

```json
{
  "type": "response",
  "id": "req-42",
  "success": false,
  "error": "not found",
  "code": "not_found"
}
```

`id` is echoed when it is a string of at most 128 characters or an integer; otherwise it is `null`. `error` is human-readable; branch on `code`:

| `code` | Meaning |
|--------|---------|
| `bad_request` | Malformed JSON, binary frame, unknown action, missing `flightId`, or a non-numeric / non-finite `since` / `until` |
| `not_found` | `fetchFlight` found no such flight |
| `rate_limited` | Per-connection rate limit exceeded by more than the server will delay; retry later |
| `history_unavailable` | The SQLite archive is configured but cannot be read |
| `live_unavailable` | The Redis live store is unreachable |
| `internal_error` | Unexpected server error (logged with a traceback) |

**RPC action reference**

| Action | Parameters | Return payload | Errors and edge cases |
|--------|------------|----------------|-----------------------|
| `subscribe` | `streams`: Array of stream names (`["flights", "alerts"]`) | `{"streams": [...]}` containing accepted stream names | Invalid streams return `success: false, error: "Unknown streams"`. Beast is not a live stream. |
| `fetchFlights` | `view`: `"live"` or `"history"`<br/>`skip`: Integer offset (0 to 10000)<br/>`limit`: Page size (1 to 200, default 50)<br/>`q`: Search substring for ICAO, callsign, or flight ID<br/>`since`: Minimum unix epoch seconds on flight end time<br/>`until`: Maximum unix epoch seconds on flight end time | Array of flight summary objects matching the filter | Clamped to max 200 items per call. Live view ignores `skip` and pagination. |
| `fetchFlight` | `flightId`: Flight ID string (required)<br/>`view`: `"live"` or `"history"` (default `"live"`) | Flight detail document enriched with airframe metadata and photo links | Missing `flightId` returns error; non-existent flight returns `success: false, error: "not found"`. |
| `fetchTelemetry` | `flightId`: Flight ID string (required)<br/>`view`: `"live"` or `"history"`<br/>`since`: Unix epoch seconds (default 0.0)<br/>`limit`: Max points (history, 1 to 20000) | Array of chronological telemetry sample points | Live view pulls from Redis sorted set within TTL; history view reads SQLite and returns at most 20000 points. |
| `fetchAlerts` | `view`: `"live"` or `"history"`<br/>`flightId`: Filter by flight ID<br/>`rule`: Filter by rule name<br/>`q`: Search query<br/>`active_only`: Boolean filter<br/>`since` / `until`: Epoch bounds<br/>`skip` (0 to 10000) / `limit` (0 to 200) | Array of alert episode records | History search indexes zone names, rules, callsigns, and ICAO codes. |
| `fetchStats` | None | Object containing flight and alert counts, storage health, and engine heartbeat | `engine_live` is `false` and live counts are 0 when the engine heartbeat is stale; `engine_seen_at` is `null` if no heartbeat was ever written. |
| `fetchZones` | None | Object containing station `home` coordinates, polygon geometries, and `alert_colors` | Returns empty arrays if no geofence zones are configured in YAML. |
| `fetchConfig` | None | Object containing station coordinates, `remember_planes`, configured `receivers`, and Beast bind details | Exposes non-sensitive runtime parameters needed for frontend coordinate rendering. |

**Beast sensor stream (`/ws/beast` and TCP)**

PyAerial does not forward dump1090's Beast socket verbatim. Frames from every configured receiver are merged first (exact hex keeps the stronger RSSI; near-identical payloads from different receivers within 100 ms collapse by Hamming distance ≤ 2 bits), then re-encoded as dump1090 Beast binary:

```
<0x1a> <type> <6-byte 12 MHz clock> <1-byte signal> <Mode S payload>
```

`type` is `0x31` (Mode A/C, 2 bytes), `0x32` (Mode S short, 7 bytes), or `0x33` (Mode S long, 14 bytes). Any `0x1a` byte in the timestamp, signal, or payload is doubled. `rssi` is packed as dump1090-fa's `sqrt(signalLevel)*255` byte. The 48-bit Beast clock is always derived from engine receive time so two receivers share one time base (hardware 12 MHz counters are not mixed on the wire).

`/ws/beast` is a binary WebSocket: no JSON `hello`, no RPC, no ping. Connect and read Beast frames. Browser origins follow the same origin policy as `/ws/live`; native clients that omit `Origin` are accepted. `/ws/beast` connections count toward the WebSocket connection caps.

The same bytes are offered on TCP when `web.beast_port` is set (default `30005`, bind `web.beast_host`, default `127.0.0.1`; set `0.0.0.0` or `PYAERIAL_BEAST_HOST=0.0.0.0` to accept feeders from other hosts or containers). At most `web.beast_max_clients` (default 16) feeders may be connected at once. That listener is what OpenSky Network's feeder expects (`BEASTHOST` / `BEASTPORT`). Set `web.beast_port: null` or `PYAERIAL_BEAST_PORT=off` if port 30005 is already taken by dump1090 on the same host; `/ws/beast` still serves.

OpenSky docker feeder example, pointing at this process instead of dump1090:

```yaml
opensky:
  image: ghcr.io/sdr-enthusiasts/docker-opensky-network:latest
  environment:
    - BEASTHOST=pyaerial_web   # or the host running `pyaerial web`
    - BEASTPORT=30005
    - LAT=35.727488
    - LONG=-78.695942
    - ALT=120
    - OPENSKY_USERNAME=youruser
```

**Client implementation (Python)**

The following script connects to `/ws/live`, filters for flight and alert streams, processes incoming telemetry, and queries detailed flight records using the RPC mechanism:

```python
import asyncio
import json
import websockets

SERVER_URI = "ws://127.0.0.1:10090/ws/live"
 
async def monitor_flights():
    async with websockets.connect(SERVER_URI) as ws:
        handshake = json.loads(await ws.recv())
        assert handshake.get("type") == "hello", "Invalid server handshake"

        # Subscribe exclusively to flights and alerts
        await ws.send(json.dumps({
            "type": "request",
            "id": "sub-1",
            "action": "subscribe",
            "params": {"streams": ["flights", "alerts"]}
        }))

        while True:
            raw_message = await ws.recv()
            message = json.loads(raw_message)
            msg_type = message.get("type")

            if msg_type == "flights":
                for flight in message.get("flights", []):
                    icao = flight.get("icao")
                    callsign = flight.get("callsign") or "UNAVAILABLE"
                    lat = flight.get("latitude")
                    lon = flight.get("longitude")
                    alt = flight.get("altitude")
                    print(f"TRACK {icao} [{callsign}]: pos=({lat}, {lon}) alt={alt}m")

            elif msg_type == "alerts":
                for alert in message.get("alerts", []):
                    if alert.get("active"):
                        print(f"ALERT ACTIVE: zone={alert.get('zone')} rule={alert.get('rule')} icao={alert.get('icao')} eta={alert.get('eta')}s")

            elif msg_type == "response" and message.get("id") == "sub-1":
                print(f"Subscription confirmed: {message.get('data')}")

if __name__ == "__main__":
    try:
        asyncio.run(monitor_flights())
    except KeyboardInterrupt:
        pass
```

**Client implementation (TypeScript)**

The following Node.js script connects to `/ws/live`, processes real-time flight updates, and executes a paginated historical flight search:

```typescript
import WebSocket from 'ws';

interface ServerMessage {
  type: string;
  id?: string;
  success?: boolean;
  flights?: Array<Record<string, unknown>>;
  alerts?: Array<Record<string, unknown>>;
  data?: unknown;
  error?: string;
}

const WS_URL = 'ws://127.0.0.1:10090/ws/live';
 
const ws = new WebSocket(WS_URL);

ws.on('open', () => {
  console.log('Connected to PyAerial WebSocket gateway');

  // Query the 10 most recent flights from SQLite history
  const historyQuery = {
    type: 'request',
    id: 'query-history',
    action: 'fetchFlights',
    params: {
      view: 'history',
      skip: 0,
      limit: 10
    }
  };
  ws.send(JSON.stringify(historyQuery));
});

ws.on('message', (data: WebSocket.RawData) => {
  const msg: ServerMessage = JSON.parse(data.toString());

  switch (msg.type) {
    case 'hello':
      console.log(`Server protocol: ${msg.type}, version: ${(msg as any).version}`);
      break;

    case 'flights':
      console.log(`Live flight count: ${msg.flights?.length ?? 0}`);
      break;

    case 'response':
      if (msg.id === 'query-history' && msg.success) {
        const flights = msg.data as Array<{ icao: string; callsign: string; flight_id: string }>;
        console.log(`Retrieved ${flights.length} historical flights:`);
        flights.forEach((f) => console.log(` - ${f.flight_id}: ${f.icao} (${f.callsign || 'N/A'})`));
      }
      break;

    case 'ping':
      break;

    default:
      console.log(`Event: ${msg.type}`);
  }
});

ws.on('close', (code, reason) => {
  console.log(`Connection closed: ${code} (${reason.toString()})`);
});
```
