# CLI

`pyaerial` operates the tracking pipeline, configuration validator, historical database purger, and real-time web portal.

**Subcommands**

| Subcommand | Process role |
|------------|--------------|
| `pyaerial run` | Tracking engine (ingests frames, writes Redis live state and SQLite history) |
| `pyaerial web` | Read-only API server and web portal (reads Redis and SQLite; does not track) |
| `pyaerial validate` | Syntax, schema, and filesystem cross-reference verification |
| `pyaerial reset` | Retention purger (`--yes`; optional single ICAO; clears Redis only when engine is stopped) |

The web portal serves `GET /health`, `GET /ready`, and `GET /api` schema endpoints alongside WebSocket telemetry feeds. Live flight state broadcasts over `ws://<host>:<port>/ws/live` (with `/ws` supported as an alias), whereas high-volume RF sensor frames stream exclusively over `/ws/raw`. When `web.token` or `PYAERIAL_WEB_TOKEN` is configured, client connections must supply the token through a `?token=` query parameter.

**Command execution**

```bash
# Start tracking engine with custom config and aircraft metadata cache
pyaerial run -c /path/to/config.yaml --aircraft-db /path/to/aircraft.db

# Validate configuration syntax and referenced geometries without starting services
pyaerial validate -c config.yaml

# Start web portal bound to all network interfaces
pyaerial web -c config.yaml --host 0.0.0.0 --port 10090

# Purge all retained flights, tracks, and alerts from SQLite
pyaerial reset --yes

# Purge history for a single ICAO target
pyaerial reset --yes abc123

# Stream recorded hex capture through engine (paths resolve relative to replay.yaml)
pyaerial run -c src/pyaerial/examples/replay.yaml
```

**Environment overrides**

Runtime settings accept environment variable overrides; explicit `-c` flags take precedence over `PYAERIAL_CONFIG`.

| Variable | Target configuration key | Behavior |
|----------|--------------------------|----------|
| `PYAERIAL_CONFIG` | Config path | Default path when `-c` is omitted |
| `PYAERIAL_HISTORY` | `database.path` | SQLite history database file path |
| `PYAERIAL_AIRCRAFT_DB` | `--aircraft-db` default | HexDB / Planespotters cache path (`aircraft.db`) |
| `PYAERIAL_REDIS` | `database.redis_uri` | Redis connection URI |
| `PYAERIAL_LOG_LEVEL` | `logging.level` | Logging severity (`debug`, `info`, `warning`, `error`) |
| `PYAERIAL_LOG_FILE` | `logging.file` | Destination log file path |
| `PYAERIAL_HZ` | `tracking.hz` | Engine evaluation frequency in Hz |
| `PYAERIAL_WEB_TOKEN` | `web.token` | Shared secret token for `/ws/live` and `/ws/raw` |
| `PYAERIAL_WEB_ORIGINS` | `web.origins` | Allowed browser origins (`*` permits all) |

Configuration strings support `${VAR}` and `${VAR:-default}` environment interpolation, but referenced variables without defaults must be set in the host environment or startup validation fails.

```yaml
on_activate:
  - method: webhook
    options:
      url: "${PYAERIAL_WEBHOOK_URL}"
      format: discord
```

**WebSocket live interface (`/ws/live`)**

Browser applications on distinct hosts require matching origins in `web.origins` (`*` by default), whereas native non-browser clients connect unrestricted without origin headers. Upon connecting to `/ws/live`, the server sends a `hello` handshake (`protocol: pyaerial.live`) followed immediately by snapshots for `flights`, `alerts`, and `stats`, after which it pushes incremental updates alongside `telemetry` and periodic keepalive `ping` frames. The machine-readable interface schema is queryable via `GET /api`.

```python
import asyncio, json, websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:10090/ws/live") as ws:
        hello = json.loads(await ws.recv())
        assert hello["type"] == "hello"
        await ws.send(json.dumps({
            "type": "request", "id": "1",
            "action": "subscribe",
            "params": {"streams": ["flights", "alerts"]},
        }))
        await ws.send(json.dumps({
            "type": "request", "id": "2",
            "action": "fetchFlights",
            "params": {"view": "live"},
        }))
        while True:
            msg = json.loads(await ws.recv())
            print(msg["type"], msg.get("id") or msg.get("stats") or "")

asyncio.run(main())
```

**RPC request and response format**

```json
// Client request
{ "type": "request", "id": "1", "action": "fetchFlights", "params": { "view": "live" } }

// Server response
{ "type": "response", "id": "1", "success": true, "data": [] }
```

**WebSocket actions**

| Action | Parameters | Semantics |
|--------|------------|-----------|
| `subscribe` | `streams` (`flights`, `alerts`, `telemetry`, `stats`) | Filters active push streams on this connection. Default or `[]` enables all four. `raw` is rejected; use `/ws/raw`. |
| `fetchFlights` | `view` (`live` or `history`), `skip`, `limit`, `q`, `since`, `until` | Historical `q` filters on ICAO, callsign, or flight ID. `since`/`until` filter epoch seconds on `end_time`. Max limit: 200. |
| `fetchFlight` | `flightId`, `view` | Returns single flight document. Returns `success: false` if missing. |
| `fetchTelemetry` | `flightId`, `view`, `since` | Returns track coordinate points recorded after `since` epoch seconds. |
| `fetchAlerts` | `view`, `skip`, `limit`, `q`, `since`, `until`, `flightId`, `rule` | Historical `q` filters across ICAO, callsign, zone, rule, or flight ID. |
| `fetchStats` | None | Returns active/retained flight counts, store connectivity flags, and engine heartbeat timestamp. |
| `fetchZones` | None | Returns home coordinates, polygon geometries, and `alert_colors`. |
| `fetchConfig` | None | Returns active UI display configuration. |

**Raw sensor interface (`/ws/raw`)**

`/ws/raw` isolates raw RF frames using the `pyaerial.raw` subprotocol and rejects live RPC actions. Connecting clients receive a `hello` handshake, an `antenna` message containing receiver coordinates and configurations, followed by continuous `raw` message batches.

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
      "clock": 123456
    }
  ]
}
```

While standard AVR text (`*HEX;` on port 30002) supplies unadorned hex payloads, Beast binary (`format: beast` on port 30005) and timestamped AVR (`@CLOCKHEX;`) populate `rssi` in dBFS and a 12 MHz hardware sample counter (`clock`, where 1 tick ≈ 83.33 ns). Downlink format `df` and transponder `icao` are populated whenever DF17 or DF18 messages are decoded, and `timestamp` reflects engine reception time in unix epoch seconds.

Comprehensive message schemas, RPC actions, and client integration libraries are detailed in [WEBSOCKET.md](WEBSOCKET.md).
