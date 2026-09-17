# CLI

`pyaerial` is the only command-line entry point.

| Subcommand | Role |
|------------|------|
| `pyaerial run` | Tracking engine (writes Redis and SQLite) |
| `pyaerial web` | Portal (reads Redis and SQLite; does not track) |
| `pyaerial validate` | Config syntax, schema, and cross-references |
| `pyaerial view` | Interactive viewer (`list`, `dump aircraft`, `status`, `live`) |
| `pyaerial live` | ASCII terminal table |

`pyaerial web` serves `GET /health`, `GET /ready`, `GET /api` (protocol discovery), and a WebSocket at `ws://…/ws/live` (alias `/ws`). Raw receiver frames are only on `/ws/raw` (not on `/ws/live`). Other clients can consume the live socket and request history. Pass `?token=` when `web.token` or `PYAERIAL_WEB_TOKEN` is set.

**Commands**

```bash
pyaerial run -c /path/to/config.yaml --aircraft-db /path/to/aircraft.db

pyaerial validate -c config.yaml

# Requires `pyaerial run` + Redis
pyaerial web -c config.yaml --host 0.0.0.0 --port 10090

pyaerial live --interval 2.0
pyaerial live --once

pyaerial view [-c config.yaml]

pyaerial run -c src/pyaerial/examples/replay.yaml
```

**Environment overrides**

Environment variables override values in `config.yaml`. The `-c` flag still takes precedence over `PYAERIAL_CONFIG`.

| Variable | Config key | Notes |
|----------|------------|-------|
| `PYAERIAL_CONFIG` | Config path | Default file when `-c` is omitted |
| `PYAERIAL_HISTORY` | `database.path` | SQLite history file |
| `PYAERIAL_REDIS` | `database.redis_uri` | Redis URI |
| `PYAERIAL_LOG_LEVEL` | `logging.level` | `debug`, `info`, `warning`, `error` |
| `PYAERIAL_LOG_FILE` | `logging.file` | Log file path |
| `PYAERIAL_HZ` | `tracking.hz` | Engine tick rate (Hz) |
| `PYAERIAL_WEB_TOKEN` | `web.token` | Shared secret for `/ws/live` and `/ws/raw` |
| `PYAERIAL_WEB_ORIGINS` | `web.origins` | Comma-separated origins (`*` = any) |

YAML strings also expand `${VAR}` and `${VAR:-default}`. A referenced variable with no default must be set, or `pyaerial validate` / load fails.

```yaml
on_activate:
  - method: webhook
    options:
      url: "${PYAERIAL_WEBHOOK_URL}"
      format: discord
```

The full YAML schema is in [CONFIGURATION.md](CONFIGURATION.md).

**WebSocket**

Connect to `ws://<host>:<port>/ws/live` (or `/ws`). Native clients with no `Origin` header are accepted. Browser apps on another host need `web.origins: ["*"]` or an explicit list (the default is `*`). If `web.token` is set, pass it as `?token=` or the `x-pyaerial-token` header.

`GET /api` returns the same protocol document the socket sends on connect.

On connect `/ws/live` sends `hello`, then a snapshot of `flights`, `alerts`, and `stats`. After that it pushes those streams plus `telemetry` and `ping`. Raw frames are not on this socket: connect to `/ws/raw`. The engine publishes frames on Redis `live:raw`. RSSI and `clock` are present when dump1090 is read in Beast format.

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

**Request**

```json
{ "type": "request", "id": "1", "action": "fetchFlights", "params": { "view": "live" } }
```

**Reply**

```json
{ "type": "response", "id": "1", "success": true, "data": [] }
```

| Action | Params | Notes |
|--------|--------|-------|
| `subscribe` | `streams` (`flights`, `alerts`, `telemetry`, `stats`) | Omit or pass `[]` for the default set. `raw` is not a live stream; use `/ws/raw`. |
| `fetchFlights` | `view` (`live` or `history`); history also takes `skip`, `limit`, `q`, `since`, `until` | History `q` matches ICAO, callsign, or flight id. `since` / `until` are unix seconds on `end_time`. |
| `fetchFlight` | `flightId`, `view` | Single flight |
| `fetchTelemetry` | `flightId`, `view`, `since` | Track points after `since` |
| `fetchAlerts` | `view`; history also takes `skip`, `limit`, `q`, `since`, `until`, `flightId`, `rule` | History `q` matches ICAO, callsign, zone, rule, or flight id |
| `fetchStats` | none | Live and retained counts, `redis` / `history` booleans, `engine_seen_at` |
| `fetchZones` | none | Home, polygons, `alert_colors` |
| `fetchConfig` | none | Portal display config |

Pushed `/ws/live` message types are `hello`, `flights`, `alerts`, `telemetry`, `stats`, and `ping`. `/ws/raw` sends `hello`, `antenna`, `raw`, and `ping`.

**Raw sensor stream**

`/ws/raw` sends `hello`, then `antenna` (home lat/lon and configured receivers), then `raw` batches as the engine sees frames:

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

`rssi` (dBFS) and `clock` (dump1090 12 MHz ticks: 48-bit integer, 1 tick = 1/12 000 000 s ≈ 83.3 ns; not unix time) appear when the dump1090 receiver uses Beast (`format: beast`, typically port 30005) or timestamped AVR (`@CLOCKHEX;`). Classic AVR `*HEX;` on port 30002 is hex-only. `df` and `icao` are filled for DF17/18 frames. `timestamp` on each message is unix seconds (engine receive time).
