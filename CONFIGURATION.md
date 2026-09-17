# Configuration

Configuration is stored in YAML format. See [`config.yaml`](config.yaml) and [`src/pyaerial/examples/config.yaml`](src/pyaerial/examples/config.yaml).

Environment variables can override selected keys; see [CLI.md](CLI.md#environment-variable-overrides). String values in `config.yaml` also expand `${VAR}` and `${VAR:-default}`. A referenced variable with no default must be set, or `pyaerial validate` / load fails.

## Section Breakdown

| Section        | Description                                                                           |
|----------------|---------------------------------------------------------------------------------------|
| `database`     | SQLite history file path and Redis URI                                                |
| `tracking`     | Tick rate, plane retention, live telemetry window, ETA options, status reporting      |
| `logging`      | Log level and optional file logging                                                   |
| `home`         | Receiver station latitude & longitude for ADS-B CPR decode (not the geofence)         |
| `receivers`    | Named receiver instances (`dump1090`, `py1090`, `replay`)                             |
| `zones`        | Named polygons plus independent constraint rules (not an implicit inside-test)        |
| `alert_colors` | Hex colors keyed by **rule name**                                                     |
| `web`          | Optional `token` for `/ws/live`, and `origins` (default `*`) for cross-origin clients |

## Configuration Example

```yaml
database:
  path: pyaerial.db               # SQLite archive of retained flights
  redis_uri: redis://localhost:6379/0

tracking:
  hz: 2                           # Main loop frequency (Hz)
  remember_planes: 120            # Seconds to retain idle planes in RAM
  backdate_packets: 10            # Position history depth for velocity calculations
  duplicate_packet_merging: 5     # Seconds window to dedup duplicate hex frames
  status_message_top_planes: 5    # Top planes to show in console status lines
  advanced_status: true
  use_kalman_eta: false           # Use Kalman-smoothed velocity for ETA
  curved_projection: false        # Turn-rate-aware curved-path ETA projection
  telemetry_keep_seconds: 600     # How long live track points are kept in Redis

logging:
  level: info
  # file: /var/log/pyaerial.log

home:
  latitude: 35.727488
  longitude: -78.695942

alert_colors:
  warn: "#f59e0b"
  alert: "#ef4444"

# web:
#   token: "shared-secret"

receivers:
  main:
    type: dump1090
    host: localhost
    port: 30002
  sdr:
    type: py1090
    options:
      rtl_index: "0"
  # recorded:
  #   type: replay
  #   options:
  #     path: captures/adsb.raw   # lines of hex, or `timestamp hex`
  #     speed: 1.0
  #     loop: true

zones:
  airport_approach:
    color: "#f59e0b"
    coordinates:
      - [35.7288, -78.6954]
      - [35.7303, -78.6965]
      - [35.7304, -78.6992]
      - [35.7288, -78.6954]
    # file: zones/approach.geojson   # instead of coordinates; .kml / .kmz / .geojson
    rules:
      - name: low_altitude_warning
        color: "#ef4444"
        when:
          altitude: { max: 1000 }      # Altitude constraint (meters)
          eta: { max: 120 }             # Estimated arrival time constraint (seconds)
        dwell_seconds: 60              # Hold before activate (if hysteresis is 0) and before retain
        retain: true                   # If false, matching this rule never archives the flight
        hysteresis_seconds: 0          # If >0, overrides dwell as the activation hold; also the off-delay
        # predict_seconds: 20          # Also match against dead-reckoned future state
        on_activate:
          - method: print
          - method: webhook
            options:
              url: "https://example.com/alerts"
        on_deactivate:
          - method: print
        while_active:
          interval_seconds: 30
          actions:
            - method: print
```

Use `${VAR}` expansion for webhook secrets:

```yaml
on_activate:
  - method: webhook
    options:
      url: "${PYAERIAL_WEBHOOK_URL}"
      format: discord
```

## Rule Field Constraints

A zone is a named polygon plus independent rules. A rule fires when every `when` constraint holds. Include `eta`, `distance`, or `proximity` to tie a rule to the zone.

The `when` section supports numeric constraints (`min` / `max`) on telemetry and calculated metrics:

| Metric Field                    | Unit          | Description                                                                 |
|---------------------------------|---------------|-----------------------------------------------------------------------------|
| `altitude` / `alt`              | Metres (`m`)  | Altitude (converted from feet)                                              |
| `speed` / `horizontal_speed`    | km/h          | Ground speed (ADS-B knots × 1.852, or geodesic)                             |
| `vertical_speed` / `vert_speed` | m/s           | Rate of climb/descent (from ft/min)                                         |
| `distance` / `dist`             | km            | Geodesic distance to the **zone polygon** edge                              |
| `proximity`                     | m             | Same as `distance`, in metres                                               |
| `heading` / `direction`         | Degrees (`°`) | Course; wrapping windows like `{min: 350, max: 10}` work                    |
| `eta`                           | Seconds (`s`) | Time along the projected track to the zone boundary (`0` if already inside) |

See [UNITS.md](UNITS.md) for stored units and conversion sources.

Each rule also accepts:

| Field                | Default  | Description                                                                       |
|----------------------|----------|-----------------------------------------------------------------------------------|
| `dwell_seconds`      | required | Minimum seconds `when` must hold to activate (when hysteresis is 0) and to retain |
| `retain`             | `true`   | If `false`, matching this rule never archives the flight                          |
| `hysteresis_seconds` | `0`      | If >0, activation hold (overrides dwell). Also the deactivation off-delay         |
| `predict_seconds`    | unset    | Also evaluate `when` against a position this many seconds ahead                   |

Zone polygons are `[latitude, longitude]` rings, or a `file` path relative to the config (`.kml`, `.kmz`, `.geojson` / `.json`). Provide `coordinates` or `file`, not both. GeoJSON/KML use lon,lat internally; PyAerial converts to lat,lon.

Replay receiver `options`: `path` (required), `speed` (default `1.0`), `loop` (default `true`), `interval` (seconds between untimestamped lines, default `0.1`). See [`src/pyaerial/examples/replay.yaml`](src/pyaerial/examples/replay.yaml).

---

## Data Model & Storage

### Redis (Live Telemetry & Active State)

Redis serves as an in-memory buffer while flights are active.
- `live:flights`: set of active flight ids
- `live:flight:{flight_id}`: current aircraft state JSON document
- `live:telemetry:{flight_id}`: sorted set of recent track points
- `live:alerts:{flight_id}`: alert episodes for that flight
- `live:active_alerts` / `live:alert_episodes`: global active set and episode index
- `live:engine`: tracking-engine heartbeat (`seen_at`); expires if `pyaerial run` stops

Data is automatically cleared or transitioned when a flight expires from memory.

### SQLite (Historical Retention)

When a flight expires from the live store it is written to the SQLite file at `database.path` only if **retain** says so:

1. A recorded alert episode whose rule has `retain: true` lasted at least `dwell_seconds`, or
2. Reconstructing the track against a `retain: true` rule shows at least `dwell_seconds` of matching samples.

A rule with `retain: false` never archives a flight on its own. Live Redis keys are still written for every active episode.

The engine and web portal share one file (WAL mode). Relative paths resolve against the config file directory. ICAO metadata stays in a separate `aircraft.db` cache; it is not stored in Redis or in this archive.

| Table       | Purpose                                                                                                      |
|-------------|--------------------------------------------------------------------------------------------------------------|
| `flights`   | Retained flight summary rows (ICAO, callsign, start/end times)                                               |
| `telemetry` | Time-series track points linked by `flight_id`                                                               |
| `alerts`    | Recorded alert episodes detailing zone name, rule name, activation/deactivation times, and position          |

Flight IDs follow the format: `{icao}-{first_packet_timestamp}` (e.g. `a1b2c3-1721832000`).

The historical portal view pages flights and alerts (50 at a time), searches ICAO / callsign / flight id on the server, and can filter by end date.

In `pyaerial view`, `dump aircraft <icao>` prints the HexDB / Planespotters cache record (`dump opensky` remains an alias).
