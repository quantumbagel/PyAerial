# PyAerial

_scanning software for ADS-B / Mode S — airstrik 2.0, rev 2_

PyAerial listens for nearby aircraft using the Mode S / ADS-B protocol, tracks them in memory, evaluates user-defined geofences, fires alerts, buffers live flights in Redis for the web portal, and persists retained completed flights to MongoDB.

Version 2 is an installable Python package with typed configuration, plugin registries for receivers and alerters, centralized logging, and a CLI.

## Features

- ADS-B decoding for position, altitude, velocity, callsign, and more ([The 1090 Megahertz Riddle](https://mode-s.org/decode))
- Multiple simultaneous receivers (e.g. dump1090 TCP + RTL-SDR)
- Geofence rules with field constraints and ETA-based alerting
- Pluggable alerters (`print`, `kafka`)
- Redis live store for in-flight flights, telemetry, and alert events
- MongoDB history store for retained completed flights
- Web portal with Live and Historical views
- SQLite-backed aircraft metadata index with API lookup and caching
- Validated YAML configuration with `pyaerial validate`
- Interactive saved & live flight viewer via `pyaerial view` (alias: `statview`)
- Dump1090-like live flight terminal display via `pyaerial dump1090` (alias: `live`)

## Installation

Dependencies are declared in [`pyproject.toml`](pyproject.toml).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .              # core (dump1090 receiver, print alerter)
pip install -e ".[all]"       # + RTL-SDR receiver and Kafka alerter
```

Optional extras:

| Extra | Packages | Enables |
|-------|----------|---------|
| `sdr` | `pyrtlsdr`, `numpy` | `py1090` RTL-SDR receiver |
| `kafka` | `kafka-python-ng` | Kafka alerter |
| `all` | both | full feature set |


## Quick start

1. Edit `config.yaml` (see [Configuration](#configuration) below).
2. Start MongoDB and Redis (or use `docker compose up`).
3. Start a message source — for dump1090:

   ```bash
   dump1090 --net --raw
   ```

4. Run PyAerial:

   ```bash
   pyaerial run -c config.yaml
   ```

5. Open the portal:

   ```bash
   pyaerial web -c config.yaml
   ```

Validate a config without running:

```bash
pyaerial validate -c config.yaml
```

Browse saved and live flights from the CLI:

```bash
pyaerial view -c config.yaml [--mock]
```

Display live tracking in dump1090 style:

```bash
pyaerial dump1090 --mock
```

## CLI

| Command | Description |
|---------|-------------|
| `pyaerial run` | Start the tracking engine |
| `pyaerial web` | Start the web portal |
| `pyaerial validate` | Check configuration syntax and cross-references |
| `pyaerial view` | Interactive flight viewer for saved & live data (alias: `statview`) |
| `pyaerial dump1090` | Terminal live flight table display (alias: `live`) |



Environment overrides (applied on top of the config file):

| Variable | Config key |
|----------|------------|
| `PYAERIAL_MONGODB` | `database.uri` |
| `PYAERIAL_REDIS` | `database.redis_uri` |
| `PYAERIAL_LOG_LEVEL` | `logging.level` |
| `PYAERIAL_LOG_FILE` | `logging.file` |
| `PYAERIAL_HZ` | `tracking.hz` |

## Configuration

See [`config.yaml`](config.yaml) and [`src/pyaerial/examples/config.yaml`](src/pyaerial/examples/config.yaml) for reference configs.

### Top-level sections

| Section | Purpose |
|---------|---------|
| `database` | MongoDB and Redis connection URIs (optional MongoDB database name) |
| `tracking` | Tick rate, plane retention, deduplication, status output |
| `logging` | Log level and optional log file |
| `home` | Receiver position for ADS-B position decoding |
| `receivers` | Named receiver instances |
| `zones` | Geofences and their alert/retain rules |

### Database

```yaml
database:
  uri: mongodb://localhost:27017
  redis_uri: redis://localhost:6379/0
  # name: pyaerial   # optional; defaults to URI path or pyaerial
```

### Tracking and logging

| Key | Default | Description |
|-----|---------|-------------|
| `tracking.hz` | `2` | Maximum main-loop tick rate |
| `tracking.remember_planes` | `30` | Seconds to keep idle planes in RAM |
| `tracking.backdate_packets` | `10` | Position history depth for speed/heading |
| `tracking.duplicate_packet_merging` | `5` | Dedup window for identical message hex |
| `logging.level` | `info` | Log level (`debug`, `info`, `warning`, `error`) |
| `logging.file` | _(none)_ | Optional rotating log file path |

### Receivers

```yaml
receivers:
  main:
    type: dump1090
    host: localhost
    port: 30002
  sdr:
    type: py1090
    options:
      rtl_index: "0"
```

Built-in receivers: `dump1090` (TCP stream), `py1090` (RTL-SDR, requires `pyrtlsdr`).

### Zones and rules

Each zone defines a polygon and one or more rules. A rule becomes **active** while its `when` constraints pass and **inactive** when they stop. Lifecycle hooks run alerters on transitions:

- `on_activate` — when conditions first match
- `on_deactivate` — when conditions stop matching
- `while_active` — periodic actions while the rule stays active (`interval_seconds` + `actions`)

`dwell_seconds` controls Mongo retention only (seconds of matching ticks required to keep an unalerted flight).

```yaml
zones:
  main:
    coordinates: [[35.75, -78.90], ...]
    rules:
      - name: warn
        when:
          altitude: { max: 1000 }
        dwell_seconds: 60
        on_activate:
          - method: print
        retain: true
```

Field constraints use `min` / `max` on telemetry fields such as `altitude`, `horizontal_speed`, `direction`, `distance`, and `eta`.

## Data model

### Redis (live)

Live flights, telemetry points, active alerts, and alert episodes are buffered in Redis while a flight is active. Keys are cleared when the flight expires.

### MongoDB (historical)

Retained flights are written once on flight end (when alerts fired or a `retain: true` rule matched).

| Collection | Purpose |
|------------|---------|
| `flights` | Retained completed flight documents |
| `telemetry` | Track points keyed by `flight_id` + `timestamp` |
| `alerts` | Alert episodes with zone, rule, activated/deactivated times, and position |

Flight IDs use the form `{icao}-{first_packet_timestamp}` for both live and historical records.

## Web portal

The portal is a React + Vite SPA (`web/`) served by a FastAPI app (`pyaerial web`, default port `10090`). All data flows through WebSocket (`/ws/live`): live updates are pushed, and queries use request/response messages on the same connection.

### Development

```bash
# Terminal 1 — API + built static (or rebuild after UI changes)
pip install -e .
pyaerial web -c config.yaml

# Terminal 2 — Vite dev server with /ws proxy
cd web && npm install && npm run dev
```

Production build (output lands in `src/pyaerial/static/`, which is generated and not committed):

```bash
cd web && npm install && npm run build
# or: ./scripts/build_web.sh
pip install -e .
pyaerial web
```

Docker builds the frontend automatically during image creation. For local editable installs, run the web build before `pyaerial web` or the portal will return HTTP 503.

### WebSocket API

Connect to `WS /ws/live`. The server pushes live snapshots and updates:

| Message type | Description |
|--------------|-------------|
| `flights` | Live flight list |
| `telemetry` | Incremental track points |
| `alerts` | Alert events |

Send JSON request messages for queries:

```json
{ "type": "request", "id": "<unique>", "action": "<action>", "params": { ... } }
```

| Action | Params | Description |
|--------|--------|-------------|
| `fetchFlights` | `view`: `live` \| `history` | Live flights (Redis) or retained history (MongoDB) |
| `fetchFlight` | `flightId`, `view` | Flight metadata |
| `fetchTelemetry` | `flightId`, `view`, `since?` | Track points |
| `fetchAlerts` | `view`, `since?`, `flightId?`, `level?`, `limit?`, `skip?` | Alert events |
| `fetchZones` | — | Home location and geofence polygons/rules |
| `fetchConfig` | — | Home location and tracking settings |

## Package layout

```
src/pyaerial/
  cli.py              CLI entrypoint
  engine.py           Main loop and receiver orchestration
  tracker.py          Plane state and deduplication
  store/              Redis live store + MongoDB history persistence
  webapp.py           FastAPI portal server (WebSocket + static SPA)
  config/             Typed schema and loader
  receivers/          Receiver plugin registry
  alerters/           Alert delivery plugin registry
  calc/               Geo math, requirement evaluation, aircraft DB
  examples/config.yaml
```

## Docker

Full stack (live SDR/dump1090 + Redis + MongoDB):

```bash
docker compose up --build
```

Runs the engine (`pyaerial`) and web portal (`pyaerial web`) against shared MongoDB and Redis instances on host networking.

Mock mode (standalone simulated feeder + web portal):

```bash
docker compose -f docker-compose.mock.yml up --build
```

or build directly:

```bash
docker build -t pyaerial-mock -f Dockerfile.mock .
docker run -p 10090:10090 pyaerial-mock
```

## Dependencies

Core (from `pyproject.toml`, installed via `pip install -e .`):

- shapely, geopy — geofence math
- pyModeS — ADS-B decoding
- pymongo — MongoDB historical persistence
- redis — live flight buffer
- pydantic, ruamel.yaml — configuration
- fastapi, uvicorn — web portal (REST, WebSocket, static SPA)
- requests — optional callsign lookup

Optional extras (`pip install -e ".[sdr]"` / `".[kafka]"` / `".[all]"`):

- pyrtlsdr, numpy — RTL-SDR receiver
- kafka-python-ng — Kafka alerter

External: `dump1090` (recommended) for the `dump1090` receiver.

## License

MIT — (c) 2024 Julian Reder (quantumbagel)
