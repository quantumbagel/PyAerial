# CLI Reference

PyAerial provides a unified command line interface via the `pyaerial` executable:

| Subcommand          | Description                                                                    |
|---------------------|--------------------------------------------------------------------------------|
| `pyaerial run`      | Start the flight tracking engine (writes Redis / MongoDB)                      |
| `pyaerial web`      | Start the web portal (reads Redis / MongoDB; does not track)                   |
| `pyaerial validate` | Check configuration file syntax, schema, and cross-references                  |
| `pyaerial view`     | Interactive terminal flight viewer (`list`, `dump aircraft`, `status`, `live`) |
| `pyaerial live`     | Real-time ASCII terminal flight display                                        |

The web portal exposes `GET /health`, `GET /ready`, `GET /api` (protocol discovery), and a WebSocket at `/ws/live` (alias `/ws`). Other apps can consume the same live stream and request history over that socket. Pass `?token=` when `web.token` / `PYAERIAL_WEB_TOKEN` is set.

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
| `PYAERIAL_MONGODB`     | `database.uri`       | MongoDB connection URI                                |
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

On connect the server sends `hello`, then a snapshot (`flights`, `alerts`, `stats`). After that it pushes those streams plus `telemetry` and `ping`.

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
| `subscribe`      | `streams` (`flights`, `alerts`, `telemetry`, `stats`)                         | Limit pushed streams for this connection. Omit / `[]` = all.                                        |
| `fetchFlights`   | `view` (`live` \| `history`); history: `skip`, `limit`, `q`, `since`, `until` | History `q` matches ICAO, callsign, or flight id. `since` / `until` are unix seconds on `end_time`. |
| `fetchFlight`    | `flightId`, `view`                                                            | Single flight detail                                                                                |
| `fetchTelemetry` | `flightId`, `view`, `since`                                                   | Track points after `since`                                                                          |
| `fetchAlerts`    | `view`; history: `skip`, `limit`, `q`, `since`, `until`, `flightId`, `rule`   | History `q` matches ICAO, callsign, zone, rule, or flight id                                        |
| `fetchStats`     | —                                                                             | Live / retained counts, `redis` / `mongo` booleans, `engine_seen_at`                                |
| `fetchZones`     | —                                                                             | Home, polygons, `alert_colors`                                                                      |
| `fetchConfig`    | —                                                                             | Portal display config                                                                               |

Pushed messages: `hello`, `flights`, `alerts`, `telemetry`, `stats`, `ping`.
