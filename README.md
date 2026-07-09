# PyAerial

_scanning software for ADS-B / Mode S — airstrik 2.0, rev 2_

PyAerial listens for nearby aircraft using the Mode S / ADS-B protocol, tracks them in memory, evaluates user-defined geofences, fires alerts, and persists eligible flights to MongoDB.

Version 2 is a full restructure into an installable Python package with typed configuration, plugin registries for receivers / savers / alerters, centralized logging, and a CLI.

## Features

- ADS-B decoding for position, altitude, velocity, callsign, and more ([The 1090 Megahertz Riddle](https://mode-s.org/decode))
- Multiple simultaneous receivers (e.g. dump1090 TCP + RTL-SDR)
- Geofence levels with boolean component requirements and ETA-based alerting
- Pluggable alerters (`print`, `kafka`) and savers (`mongo`, `print`)
- SQLite-backed aircraft metadata index (built from OpenSky `database.csv`)
- Validated YAML configuration with `pyaerial validate`
- Interactive MongoDB browser via `pyaerial statview`

## Installation

Dependencies are declared in [`pyproject.toml`](pyproject.toml).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .              # core (dump1090 receiver, mongo saver, print alerter)
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
2. Start a message source — for dump1090:

   ```bash
   dump1090 --net --raw
   ```

3. Run PyAerial:

   ```bash
   pyaerial run -c config.yaml
   ```

Validate a config without running:

```bash
pyaerial validate -c config.yaml
```

Browse saved flights:

```bash
pyaerial statview -c config.yaml
```

## CLI

| Command | Description |
|---------|-------------|
| `pyaerial run` | Start the tracking engine |
| `pyaerial validate` | Check configuration syntax and cross-references |
| `pyaerial statview` | Interactive MongoDB flight browser |
| `pyaerial build-db` | Build SQLite aircraft index from OpenSky CSV |

Environment overrides (applied on top of the config file):

| Variable | Config key |
|----------|------------|
| `PYAERIAL_MONGODB` | `general.mongodb` |
| `PYAERIAL_LOG_LEVEL` | `general.logs` |
| `PYAERIAL_LOG_FILE` | `general.log_file` |
| `PYAERIAL_HZ` | `general.hz` |

## Configuration

See [`src/pyaerial/examples/config.yaml`](src/pyaerial/examples/config.yaml) for a clean reference config.

### Top-level sections

| Section | Purpose |
|---------|---------|
| `general` | Global tuning (tick rate, MongoDB URI, saver name, logging) |
| `home` | Receiver position for ADS-B position decoding |
| `receivers` | Named receiver instances and their arguments |
| `components` | Reusable numeric constraint sets |
| `zones` | Geofences and their alert/save levels |
| `categories` | Alert method + save filters referenced by zone levels |

### General options

| Key | Default | Description |
|-----|---------|-------------|
| `mongodb` | `mongodb://localhost:27017` | MongoDB connection URI |
| `saver` | `mongo` | Saver plugin name (`mongo`, `print`) |
| `backdate_packets` | `10` | Position history depth for speed/heading |
| `remember_planes` | `30` | Seconds to keep idle planes in RAM |
| `duplicate_packet_merging` | `5` | Dedup window for identical message hex |
| `hz` | `2` | Maximum main-loop tick rate |
| `logs` | `info` | Log level (`debug`, `info`, `warning`, `error`) |
| `log_file` | _(none)_ | Optional rotating log file path |
| `status_message_top_planes` | `5` | Planes shown in status line (`-1` = all) |
| `advanced_status` | `true` | Include callsigns and packet breakdown |

### Receivers

```yaml
receivers:
  main:
    method: dump1090
    arguments:
      tcp_connection_ip: localhost
      tcp_connection_port: 30002
  sdr:
    method: py1090
    arguments:
      rtl_index: "0"
```

Built-in receivers: `dump1090` (TCP stream), `py1090` (RTL-SDR, requires `pyrtlsdr`).

### Components and requirements

Components define numeric constraints on telemetry fields. Zone levels reference them in boolean expressions using `&` (and), `|` (or), and `~` (not):

```yaml
components:
  lenient:
    altitude:
      maximum: 1000
  critical:
    altitude:
      maximum: 500

zones:
  main:
    coordinates: [[35.75, -78.90], ...]
    levels:
      warn:
        category: save_everything
        requirements: lenient
        seconds: 60
```

`seconds` is how many consecutive evaluation ticks the requirement must hold before the flight is eligible for saving.

### Categories (alert + save)

```yaml
categories:
  save_everything:
    alert_method: print          # or kafka
    arguments: {}                # kafka: {server: "localhost:9092"}
    save:
      telemetry:
        default: all             # per-field overrides possible
      calculated:
        default: all
```

Save methods: `all`, `none`, `decimate(n)`, `sdecimate(x,y)`.

### Alert payload example

```json
{
  "icao": "AD61DE",
  "callsign": "SWA1693",
  "type": "warn",
  "zone": "main",
  "eta": 52,
  "payload": {"latitude": 35.767, "longitude": -78.921, "altitude": 617.22}
}
```

## Package layout

```
src/pyaerial/
  cli.py              CLI entrypoint
  engine.py           Main loop and receiver orchestration
  tracker.py          Plane state and deduplication
  classify.py         ADS-B message classification
  models.py           Datum and data-access helpers
  config/             Typed schema + loader
  receivers/          Receiver plugin registry
  savers/             Persistence plugin registry
  alerters/           Alert plugin registry
  calc/               Geo math, requirement evaluation, aircraft DB
  examples/config.yaml
```

## Writing plugins

### Receiver

```python
from pyaerial.receivers import Receiver, register_receiver

@register_receiver("my_source")
class MyReceiver(Receiver):
    def configure(self, arguments: dict) -> None:
        self.host = arguments["host"]

    def run(self) -> str | None:
        while not self.should_stop():
            self.emit("8D4840D6202CC371C32CE0576098", time.time())
        return None
```

Register the module by importing it from `pyaerial.receivers.__init__` or ensuring it is imported before receivers start.

### Alerter

```python
from pyaerial.alerters import Alerter, register_alerter

@register_alerter("webhook")
class WebhookAlerter(Alerter):
    def alert(self, meta: dict, payload: dict) -> None:
        requests.post(self.arguments["url"], json={**meta, **payload})
```

### Saver

Subclass `pyaerial.savers.Saver`, implement `save()`, and register with `@register_saver("name")`. The base class handles flight eligibility evaluation and packet filtering.

## Docker

```bash
docker compose up --build
```

The container builds dump1090, installs PyAerial, and runs `pyaerial run`.

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
