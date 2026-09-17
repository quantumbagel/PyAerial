# CLI Reference

PyAerial provides a unified command line interface via the `pyaerial` executable:

| Subcommand          | Description                                                                    |
|---------------------|--------------------------------------------------------------------------------|
| `pyaerial run`      | Start the flight tracking engine (writes Redis / SQLite)                       |
| `pyaerial web`      | Start the web portal (reads Redis / SQLite; does not track)                    |
| `pyaerial validate` | Check configuration file syntax, schema, and cross-references                  |
| `pyaerial view`     | Interactive terminal flight viewer (`list`, `dump aircraft`, `status`, `live`) |
| `pyaerial live`     | Real-time ASCII terminal flight display                                        |

The web portal exposes `GET /health`, `GET /ready`, `GET /api` (protocol discovery), and a WebSocket at `/ws/live` (alias `/ws`). Raw dump1090 / receiver frames are on `/ws/raw` (or the opt-in `raw` stream on `/ws/live`). Other apps can consume the same live stream and request history over that socket. Pass `?token=` when `web.token` / `PYAERIAL_WEB_TOKEN` is set.

## Usage Options

```bash
# Run tracking engine with a custom config
pyaerial run -c /path/to/config.yaml --aircraft-db /path/to/aircraft.db

# Validate configuration
pyaerial validate -c config.yaml

# Launch web portal on custom host and port (requires `pyaerial run` + Redis)
pyaerial web -c config.yaml --host 0.0.0.0 --port 10090

# Live flight viewer with 2-second refresh rate
pyaerial live --interval 2.0

# Print single-frame flight snapshot and exit
pyaerial live --once

# Interactive flight search & detail view
pyaerial view [-c config.yaml]

# Replay a recorded dump1090 capture (see examples/replay.yaml)
pyaerial run -c src/pyaerial/examples/replay.yaml
```

## Environment Variable Overrides

Environment variables override values in your `config.yaml`:

| Environment Variable   | Overrides Config Key | Description                                           |
|------------------------|----------------------|-------------------------------------------------------|
| `PYAERIAL_CONFIG`      | Config path          | Default configuration file (`-c` still wins)          |
| `PYAERIAL_HISTORY`     | `database.path`      | SQLite history file path                              |
| `PYAERIAL_REDIS`       | `database.redis_uri` | Redis connection URI                                  |
| `PYAERIAL_LOG_LEVEL`   | `logging.level`      | Logging level (`debug`, `info`, `warning`, `error`)   |
| `PYAERIAL_LOG_FILE`    | `logging.file`       | Output log file path                                  |
| `PYAERIAL_HZ`          | `tracking.hz`        | Engine loop tick rate (Hz)                            |
| `PYAERIAL_WEB_TOKEN`   | `web.token`          | Optional shared secret for `/ws/live`                 |
| `PYAERIAL_WEB_ORIGINS` | `web.origins`        | Comma-separated allowed WebSocket origins (`*` = any) |

String values in `config.yaml` also expand `${VAR}` and `${VAR:-default}`. A referenced variable with no default must be set, or `pyaerial validate` / load fails. Use this for webhook secrets:

```yaml
on_activate:
  - method: webhook
    options:
      url: "${PYAERIAL_WEBHOOK_URL}"
      format: discord
```

See [CONFIGURATION.md](CONFIGURATION.md) for the full YAML schema.

## WebSocket protocol

Connect to `ws://<host>:<port>/ws/live` (or `/ws`). Native clients (no `Origin` header) are accepted. Browser apps on another host need `web.origins: ["*"]` or an explicit origin list (the default is `*`). If `web.token` is set, pass it as `?token=` or the `x-pyaerial-token` header.

`GET /api` returns the same protocol document the socket sends on connect.

On connect the server sends `hello`, then a snapshot (`flights`, `alerts`, `stats`). After that it pushes those streams plus `telemetry` and `ping`. The `raw` stream is **opt-in** (the portal does not subscribe): connect to `/ws/raw`, pass `?streams=raw`, or `subscribe` with `["raw"]`. The engine publishes frames over Redis (`live:raw`); RSSI is present when dump1090 is read in Beast format.

Python example:

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

Client request:

```json
{ "type": "request", "id": "1", "action": "fetchFlights", "params": { "view": "live" } }
```

Server reply:

```json
{ "type": "response", "id": "1", "success": true, "data": [] }
```

| Action           | Params                                                                        | Notes                                                                                               |
|------------------|-------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------|
| `subscribe`      | `streams` (`flights`, `alerts`, `telemetry`, `stats`, `raw`)                   | Limit pushed streams. Omit / `[]` = default (not `raw`). Include `raw` for sensor frames.           |
| `fetchFlights`   | `view` (`live` \| `history`); history: `skip`, `limit`, `q`, `since`, `until` | History `q` matches ICAO, callsign, or flight id. `since` / `until` are unix seconds on `end_time`. |
| `fetchFlight`    | `flightId`, `view`                                                            | Single flight detail                                                                                |
| `fetchTelemetry` | `flightId`, `view`, `since`                                                   | Track points after `since`                                                                          |
| `fetchAlerts`    | `view`; history: `skip`, `limit`, `q`, `since`, `until`, `flightId`, `rule`   | History `q` matches ICAO, callsign, zone, rule, or flight id                                        |
| `fetchStats`     | —                                                                             | Live / retained counts, `redis` / `history` booleans, `engine_seen_at`                              |
| `fetchZones`     | —                                                                             | Home, polygons, `alert_colors`                                                                      |
| `fetchConfig`    | —                                                                             | Portal display config                                                                               |

Pushed messages: `hello`, `flights`, `alerts`, `telemetry`, `stats`, `raw`, `antenna`, `ping`.

### Raw sensor stream

`/ws/raw` (or `/ws/live?streams=raw`) sends `hello`, then `antenna` (home lat/lon and configured receivers), then `raw` batches as the tracking engine sees frames:

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

`rssi` (dBFS) and `clock` (dump1090 12 MHz timestamp) are present when the dump1090 receiver uses Beast (`format: beast`, typically port 30005). AVR on port 30002 is hex-only. `df` / `icao` are filled for DF17/18 frames.
