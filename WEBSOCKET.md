# WebSocket API

PyAerial exposes real-time aircraft tracking, spatial alert notifications, historical telemetry queries, and raw Mode S receiver streams through two dedicated WebSocket interfaces.

**Endpoints and subprotocols**

| Endpoint | Subprotocol | Push streams | RPC actions | Description |
|----------|-------------|--------------|-------------|-------------|
| `ws://<host>:<port>/ws/live` | `pyaerial.live` | `flights`, `alerts`, `telemetry`, `stats`, `ping` | Supported | Main real-time application feed (alias `/ws`) |
| `ws://<host>:<port>/ws/raw` | `pyaerial.raw` | `antenna`, `raw`, `ping` | Not supported | Low-level RF frame stream directly from receivers |
| `http://<host>:<port>/api` | HTTP (JSON) | None | None | Machine-readable API discovery specification |

**Origin validation**

Browser-based connections validate the HTTP `Origin` header against entries declared in `web.origins`. Setting `web.origins: ["*"]` accepts any browser origin, while native TCP and backend clients omitting the `Origin` header connect unconditionally. Unauthorized origins are rejected with closure code `1008` (`origin not allowed`).

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

Aircraft flight list broadcast (`type: "flights"`), pushed whenever positions, velocities, or alert states update:

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
      "latitude": 35.7288,
      "longitude": -78.6954,
      "altitude": 1250.0,
      "speed": 420.5,
      "heading": 185.0,
      "is_live": true,
      "status": "live",
      "retained": false,
      "timestamp": 1721832015.2,
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

Active alerts broadcast (`type: "alerts"`), delivering the 50 most recent alert episodes:

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

Incremental telemetry points (`type: "telemetry"`), delivering only points recorded since the client's last update:

```json
{
  "type": "telemetry",
  "timestamp": 1721832015.5,
  "telemetry": [
    {
      "flight_id": "a1b2c3-1721832000",
      "latitude": 35.7288,
      "longitude": -78.6954,
      "altitude": 1250.0,
      "speed": 420.5,
      "heading": 185.0,
      "timestamp": 1721832015.2
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
    "engine_seen_at": 1721832015.0
  }
}
```

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
  "error": "not found"
}
```

**RPC action reference**

| Action | Parameters | Return payload | Errors and edge cases |
|--------|------------|----------------|-----------------------|
| `subscribe` | `streams`: Array of stream names (`["flights", "alerts"]`) | `{"streams": [...]}` containing accepted stream names | Invalid streams return `success: false, error: "Unknown streams"`. `raw` is rejected. |
| `fetchFlights` | `view`: `"live"` or `"history"`<br/>`skip`: Integer offset (0 to 100000)<br/>`limit`: Page size (1 to 200, default 50)<br/>`q`: Search substring for ICAO, callsign, or flight ID<br/>`since`: Minimum unix epoch seconds on flight end time<br/>`until`: Maximum unix epoch seconds on flight end time | Array of flight summary objects matching the filter | Clamped to max 200 items per call. Live view ignores `skip` and pagination. |
| `fetchFlight` | `flightId`: Flight ID string (required)<br/>`view`: `"live"` or `"history"` (default `"live"`) | Flight detail document enriched with airframe metadata and photo links | Missing `flightId` returns error; non-existent flight returns `success: false, error: "not found"`. |
| `fetchTelemetry` | `flightId`: Flight ID string (required)<br/>`view`: `"live"` or `"history"`<br/>`since`: Unix epoch seconds (default 0.0) | Array of chronological telemetry sample points | Live view pulls from Redis sorted set within TTL; history view reads SQLite. |
| `fetchAlerts` | `view`: `"live"` or `"history"`<br/>`flightId`: Filter by flight ID<br/>`rule`: Filter by rule name<br/>`q`: Search query<br/>`active_only`: Boolean filter<br/>`since` / `until`: Epoch bounds | Array of alert episode records | History search indexes zone names, rules, callsigns, and ICAO codes. |
| `fetchStats` | None | Object containing flight and alert counts, storage health, and engine heartbeat | Returns `engine_seen_at: null` if the tracking engine process is not actively running. |
| `fetchZones` | None | Object containing station `home` coordinates, polygon geometries, and `alert_colors` | Returns empty arrays if no geofence zones are configured in YAML. |
| `fetchConfig` | None | Object containing station coordinates and RAM timeout (`remember_planes`) | Exposes non-sensitive runtime parameters needed for frontend coordinate rendering. |

**Raw sensor stream (`/ws/raw`)**

The `/ws/raw` endpoint forwards low-level Mode S / ADS-B message frames without performing state tracking or spatial rule evaluations. Connecting clients immediately receive a `hello` handshake (`protocol: pyaerial.raw`), followed by an `antenna` message containing configured station coordinates and receiver ports:

```json
{
  "type": "antenna",
  "timestamp": 1721832000.0,
  "antenna": {
    "home": {
      "latitude": 35.727488,
      "longitude": -78.695942
    },
    "receivers": [
      {
        "name": "main",
        "type": "dump1090",
        "host": "localhost",
        "port": 30005,
        "format": "beast"
      }
    ]
  }
}
```

The server subsequently flushes frame batches as raw messages arrive from receiver threads:

```json
{
  "type": "raw",
  "timestamp": 1721832000.5,
  "messages": [
    {
      "hex": "8d406b902015a678d4d220aa4bda",
      "timestamp": 1721832000.412,
      "receiver": "main",
      "df": 17,
      "icao": "406b90",
      "rssi": -18.5,
      "clock": 123456789
    }
  ]
}
```

`rssi` represents signal power in dBFS and `clock` contains the 12 MHz free-running receiver tick counter (where 1 tick ≈ 83.33 ns), both populated when dump1090 uses Beast binary on port 30005. Standard AVR text (`*HEX;` on port 30002) populates only `hex`, `timestamp`, and `receiver`.

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
