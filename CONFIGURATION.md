# Configuration

Configuration is stored as YAML. Working copies live in [`config.yaml`](config.yaml) and [`src/pyaerial/examples/config.yaml`](src/pyaerial/examples/config.yaml).

Environment variables override selected keys; see [CLI.md](CLI.md). String values also expand `${VAR}` and `${VAR:-default}`. A referenced variable with no default must be set, or `pyaerial validate` / load fails.

| Section | Role |
|---------|------|
| `database` | SQLite history path and Redis URI |
| `tracking` | Tick rate, plane retention, live telemetry window, ETA options, status lines |
| `logging` | Log level and optional file |
| `home` | Receiver lat/lon for ADS-B CPR decode (not the geofence) |
| `receivers` | Named instances: `dump1090`, `replay` |
| `zones` | Named polygons plus independent rules (not an implicit inside-test) |
| `alert_colors` | Hex colors keyed by **rule name** |
| `web` | Optional `token` for `/ws/live` and `/ws/raw`; `origins` (default `*`) |

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
  status_interval: 10             # Seconds between info-level status log lines
  use_kalman_eta: false           # Use Kalman-smoothed velocity for ETA
  curved_projection: false        # Turn-rate-aware curved-path ETA projection
  telemetry_keep_seconds: 600     # How long live track points are kept in Redis (history keeps the full in-memory track)

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
    port: 30002          # AVR hex (`*8D...;`). Use 30005 + format: beast for RSSI.
    # format: beast      # dump1090 Beast binary; default port 30005
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
          altitude: { max: 1000 }      # metres
          eta: { max: 120 }             # seconds
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

**Webhook URL**

```yaml
on_activate:
  - method: webhook
    options:
      url: "${PYAERIAL_WEBHOOK_URL}"
      format: discord
```

**Rules**

A zone is a named polygon plus independent rules. A rule fires when every `when` constraint holds. Include `eta`, `distance`, or `proximity` to tie a rule to the zone. There is no implicit "inside the polygon" test.

`when` constraints are `min` / `max` on stored telemetry. Units are listed in [UNITS.md](UNITS.md).

| Field | Unit | Meaning |
|-------|------|---------|
| `altitude` / `alt` | m | ADS-B feet × 0.3048 |
| `speed` / `horizontal_speed` | km/h | ADS-B knots × 1.852, or geodesic |
| `vertical_speed` / `vert_speed` | m/s | ADS-B ft/min × 0.00508 |
| `distance` / `dist` | km | Geodesic to the **zone polygon** edge |
| `proximity` | m | Same as `distance`, in metres |
| `heading` / `direction` | ° | Course; wrapping windows such as `{min: 350, max: 10}` work |
| `eta` | s | Time along the projected track to the zone boundary (`0` if already inside) |

| Field | Default | Meaning |
|-------|---------|---------|
| `dwell_seconds` | required | Seconds `when` must hold to activate (when hysteresis is 0) and to retain |
| `retain` | `true` | If `false`, matching this rule never archives the flight |
| `hysteresis_seconds` | `0` | If >0, activation hold (overrides dwell) and deactivation off-delay |
| `predict_seconds` | unset | Also evaluate `when` against a position this many seconds ahead |

Polygons are `[latitude, longitude]` rings, or a `file` path relative to the config (`.kml`, `.kmz`, `.geojson` / `.json`). Provide `coordinates` or `file`, not both. GeoJSON and KML use lon,lat internally; PyAerial converts to lat,lon.

**Receivers**

dump1090 `format` is `avr` (default, TCP 30002) or `beast` (TCP 30005). Beast adds per-message RSSI (dBFS) and a 12 MHz-tick `clock` on `/ws/raw`. Port `30005` implies Beast unless `format` is set. Put `format` on the receiver or under `options`.

Replay `options` are `path` (required), `speed` (default `1.0`), `loop` (default `true`), and `interval` (seconds between untimestamped lines, default `0.1`). See [`src/pyaerial/examples/replay.yaml`](src/pyaerial/examples/replay.yaml).

**Redis**

Redis holds in-memory state while flights are active. Keys are dropped or moved when a flight expires from RAM.

| Key | Contents |
|-----|----------|
| `live:flights` | Set of active flight ids |
| `live:flight:{flight_id}` | Current aircraft state JSON |
| `live:telemetry:{flight_id}` | Sorted set of recent track points |
| `live:alerts:{flight_id}` | Alert episodes for that flight |
| `live:active_alerts` / `live:alert_episodes` | Global active set and episode index |
| `live:engine` | Heartbeat (`seen_at`); expires if `pyaerial run` stops |
| `live:raw` | Pub/sub of raw receiver frames for `/ws/raw` (not persisted) |

**SQLite**

When a flight leaves the live store it is written to `database.path` only if retain says so:

1. A recorded alert episode whose rule has `retain: true` lasted at least `dwell_seconds`, or
2. Reconstructing the track against a `retain: true` rule shows at least `dwell_seconds` of matching samples.

A rule with `retain: false` never archives a flight on its own. Live Redis keys are still written for every active episode.

The engine and portal share one file (WAL mode). Relative paths resolve against the config directory. ICAO metadata stays in a separate `aircraft.db` cache. It is not stored in Redis or in this archive.

| Table | Contents |
|-------|----------|
| `flights` | ICAO, callsign, start/end times |
| `telemetry` | Track points by `flight_id` |
| `alerts` | Zone, rule, activate/deactivate times, position |

Flight IDs use the form `{icao}-{first_packet_timestamp}`, for example `a1b2c3-1721832000`.

The historical portal pages flights and alerts 50 at a time, searches ICAO / callsign / flight id on the server, and can filter by end date.

ICAO metadata lives in `aircraft.db` (cwd, `PYAERIAL_AIRCRAFT_DB`, or `--aircraft-db`). The portal drawer reads that cache; it is not a fully offline aircraft index.
