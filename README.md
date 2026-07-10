# PyAerial

_scanning software for ADS-B / Mode S — airstrik 2.0, rev 2_

PyAerial listens for nearby aircraft using the Mode S / ADS-B protocol, tracks them in memory, evaluates user-defined geofences, fires alerts, and persists live flights, telemetry, and alert events to MongoDB for the web portal.

Version 2 is an installable Python package with typed configuration, plugin registries for receivers and alerters, centralized logging, and a CLI.

## Features

- ADS-B decoding for position, altitude, velocity, callsign, and more ([The 1090 Megahertz Riddle](https://mode-s.org/decode))
- Multiple simultaneous receivers (e.g. dump1090 TCP + RTL-SDR)
- Geofence rules with field constraints and ETA-based alerting
- Pluggable alerters (`print`, `kafka`)
- Unified MongoDB store for live flights, completed tracks, and alert events
- Web portal with live map, flight history, and alert timeline
- SQLite-backed aircraft metadata index (built from OpenSky `database.csv`)
- Validated YAML configuration with `pyaerial validate`
- Interactive MongoDB browser via `pyaerial statview`

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

Optional: build the aircraft metadata index (requires `database.csv` from OpenSky):

```bash
pyaerial build-db --csv database.csv -o aircraft.db
```

## Quick start

1. Edit `config.yaml` (see [Configuration](#configuration) below).
2. Start MongoDB (or use `docker compose up`).
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

Browse saved flights from the CLI:

```bash
pyaerial statview -c config.yaml
```

## CLI

| Command | Description |
|---------|-------------|
| `pyaerial run` | Start the tracking engine |
| `pyaerial web` | Start the web portal |
| `pyaerial validate` | Check configuration syntax and cross-references |
| `pyaerial statview` | Interactive MongoDB flight browser |
| `pyaerial build-db` | Build SQLite aircraft index from OpenSky CSV |

Environment overrides (applied on top of the config file):

| Variable | Config key |
|----------|------------|
| `PYAERIAL_MONGODB` | `database.uri` |
| `PYAERIAL_LOG_LEVEL` | `logging.level` |
| `PYAERIAL_LOG_FILE` | `logging.file` |
| `PYAERIAL_HZ` | `tracking.hz` |

## Configuration

See [`config.yaml`](config.yaml) and [`src/pyaerial/examples/config.yaml`](src/pyaerial/examples/config.yaml) for reference configs.

### Top-level sections

| Section | Purpose |
|---------|---------|
| `database` | MongoDB connection URI (and optional database name) |
| `tracking` | Tick rate, plane retention, deduplication, status output |
| `logging` | Log level and optional log file |
| `home` | Receiver position for ADS-B position decoding |
| `receivers` | Named receiver instances |
| `zones` | Geofences and their alert/retain rules |

### Database

```yaml
database:
  uri: mongodb://localhost:27017
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

Each zone defines a polygon and one or more rules. A rule fires alerts when its `when` constraints pass, and optionally retains the flight for the portal when `dwell_seconds` is satisfied or alerts were recorded.

```yaml
zones:
  main:
    coordinates: [[35.75, -78.90], ...]
    rules:
      - name: warn
        when:
          altitude: { max: 1000 }
        dwell_seconds: 60
        alert:
          method: print
        retain: true
```

Field constraints use `min` / `max` on telemetry fields such as `altitude`, `horizontal_speed`, `direction`, `distance`, and `eta`.

## Data model (MongoDB)

| Collection | Purpose |
|------------|---------|
| `flights` | One document per tracked flight (`status`: `live` or `completed`) |
| `telemetry` | Track points keyed by `flight_id` + `timestamp` |
| `alerts` | Alert event log with zone, level, position, and timestamp |

Flight IDs use the form `{icao}-{first_packet_timestamp}` for both live and historical records.

## Web portal API

| Route | Description |
|-------|-------------|
| `GET /api/flights` | Live + recent retained flights |
| `GET /api/flight?flight_id=` | Flight metadata and raw messages |
| `GET /api/telemetry?flight_id=` | Track points |
| `GET /api/live?since=` | Incremental live telemetry |
| `GET /api/alerts?since=&flight_id=&level=` | Alert events |

## Package layout

```
src/pyaerial/
  cli.py              CLI entrypoint
  engine.py           Main loop and receiver orchestration
  tracker.py          Plane state and deduplication
  store/              Unified MongoDB persistence
  webapp.py           Portal server and UI
  config/             Typed schema and loader
  receivers/          Receiver plugin registry
  alerters/           Alert delivery plugin registry
  calc/               Geo math, requirement evaluation, aircraft DB
  examples/config.yaml
```

## Docker

```bash
docker compose up --build
```

Runs the engine (`pyaerial`) and web portal (`pyaerial web`) against a shared MongoDB instance on host networking. No SQLite IPC file is required between processes.

## Dependencies

Core (from `pyproject.toml`, installed via `pip install -e .`):

- shapely, geopy — geofence math
- pyModeS — ADS-B decoding
- pymongo — MongoDB persistence
- pydantic, ruamel.yaml — configuration
- requests — optional callsign lookup

Optional extras (`pip install -e ".[sdr]"` / `".[kafka]"` / `".[all]"`):

- pyrtlsdr, numpy — RTL-SDR receiver
- kafka-python-ng — Kafka alerter

External: `dump1090` (recommended) for the `dump1090` receiver.

## License

MIT — (c) 2024 Julian Reder (quantumbagel)
