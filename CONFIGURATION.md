# Configuration

PyAerial runtime parameters, receiver inputs, spatial geofences, and alerting rules are declared in YAML. Default production templates reside at [`config.yaml`](config.yaml) and [`src/pyaerial/examples/config.yaml`](src/pyaerial/examples/config.yaml).

String values support environment variable interpolation via `${VAR}` and `${VAR:-default}` syntax, though unresolved variables lacking defaults halt engine initialization during validation. Command-line flags always take precedence over environment variables.

**Top-level sections**

| Section | Target systems |
|---------|----------------|
| `database` | SQLite archive file path and Redis connection URI |
| `tracking` | Evaluation loop frequency, in-memory deduplication, kinematics smoothing, and telemetry retention windows |
| `logging` | Log level severity (`debug`, `info`, `warning`, `error`) and optional file destination |
| `home` | Receiver coordinates used as reference point for ADS-B CPR position decoding (not geofence centers) |
| `receivers` | Named transport inputs (`dump1090`, `replay`) |
| `zones` | Named polygonal geofences and associated rule arrays |
| `alert_colors` | Hex color overrides mapped to rule names for map rendering |
| `web` | Allowed browser origins (documented in [WEBSOCKET.md](WEBSOCKET.md)) |

**Annotated configuration**

```yaml
database:
  path: pyaerial.db               # SQLite database for retained flights
  redis_uri: redis://localhost:6379/0

tracking:
  hz: 2                           # Tracking evaluation frequency
  remember_planes: 120            # Retention timeout for idle aircraft in RAM (seconds)
  backdate_packets: 10            # Track sample depth for velocity and turn-rate estimation
  duplicate_packet_merging: 5     # Frame deduplication time window (seconds)
  status_message_top_planes: 5    # Number of aircraft reported in console status lines
  advanced_status: true           # Include receiver statistics in console output
  status_interval: 10             # Interval between heartbeat status lines (seconds)
  use_kalman_eta: false           # Filter horizontal velocity with Kalman state estimation
  curved_projection: false        # Project trajectory along estimated turn rate
  telemetry_keep_seconds: 600     # Redis ring buffer retention for live map trails

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
#   origins: ["*"]

receivers:
  main:
    type: dump1090
    host: localhost
    port: 30002          # AVR text (*8D...;). Use 30005 + format: beast for RSSI.
    # format: beast      # Beast binary stream
  # recorded:
  #   type: replay
  #   options:
  #     path: captures/adsb.raw
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
    # file: zones/approach.geojson   # Alternative to inline coordinates (.kml, .kmz, .geojson)
    rules:
      - name: low_altitude_warning
        color: "#ef4444"
        when:
          altitude: { max: 1000 }      # Metres
          eta: { max: 120 }             # Seconds
        dwell_seconds: 60              # Continuous match duration required to activate and retain
        retain: true                   # Archival authorization flag
        hysteresis_seconds: 0          # Deactivation delay and activation hold override
        # predict_seconds: 20          # Dead-reckoning lookahead window
        on_activate:
          - method: print
          - method: webhook
            options:
              url: "${PYAERIAL_WEBHOOK_URL}"
              format: discord
        on_deactivate:
          - method: print
        while_active:
          interval_seconds: 30
          actions:
            - method: print
```

**Rule condition fields (`when`)**

Rules execute independently and trigger only when every condition defined in the `when` block is satisfied. Because zones represent spatial boundaries rather than implicit containment tests, a rule must explicitly evaluate `distance`, `proximity`, or `eta` against the polygon perimeter. Condition constraints are specified using numeric `min` and `max` thresholds.

| Field | Unit | Description |
|-------|------|-------------|
| `altitude` / `alt` | m | Geometric or barometric altitude |
| `speed` / `horizontal_speed` | km/h | Ground speed |
| `vertical_speed` / `vert_speed` | m/s | Climb or descent rate |
| `distance` / `dist` | km | Great-circle distance to closest polygon perimeter edge |
| `proximity` | m | `distance` converted to metres |
| `heading` / `direction` | ° | Track direction; circular ranges spanning 360° (e.g. `{min: 350, max: 10}`) are handled natively |
| `eta` | s | Travel time along projected ground track to perimeter intercept (`0` when inside) |

**Rule execution and retention controls**

| Parameter | Default | Function |
|-----------|---------|----------|
| `dwell_seconds` | Required | Continuous duration conditions must hold before triggering activation (when `hysteresis_seconds: 0`) and minimum match duration required to retain the flight |
| `retain` | `true` | When `false`, matching flights are excluded from SQLite archival regardless of dwell duration |
| `hysteresis_seconds` | `0` | When non-zero, sets both the activation hold time (overriding dwell) and the deactivation off-delay buffer |
| `predict_seconds` | Unset | Dead-reckons aircraft state forward by this duration and evaluates conditions against the projected position |

Polygon boundaries are declared as closed `[latitude, longitude]` coordinate rings or loaded from external geometries using `file: path` (`.kml`, `.kmz`, `.geojson`). Although standard GeoJSON and KML specifications store coordinates in `(longitude, latitude)` order, PyAerial normalizes all imported geometries to internal `[latitude, longitude]` pairs upon ingestion. Configurations must supply either `coordinates` or `file`, but not both.

**Receiver configurations**

TCP receivers connect to dump1090 using either `avr` (port 30002) or `beast` binary encoding (port 30005), with Beast streams forwarding `rssi` levels and 12 MHz hardware sample clocks to `/ws/raw`. Configuring port 30005 selects Beast format automatically unless overridden by explicit `format: avr` settings.

Replay receivers simulate live traffic from recorded capture files using configurable timing parameters:

| Option | Default | Function |
|--------|---------|----------|
| `path` | Required | Filesystem path to capture file (`.raw` or text containing hex lines) |
| `speed` | `1.0` | Playback speed multiplier |
| `loop` | `true` | Continuously rewind and repeat playback upon reaching EOF |
| `interval` | `0.1` | Fixed delay (seconds) inserted between lines lacking embedded timestamps |

**Live state storage (Redis)**

Redis maintains active flight buffers, live telemetry trails, and active alert episode indices.

| Redis key | Structure | Lifecycle |
|-----------|-----------|-----------|
| `live:flights` | Set | Active flight IDs; purged on flight timeout |
| `live:flight:{flight_id}` | String (JSON) | Most recent kinematic state |
| `live:telemetry:{flight_id}` | Sorted Set (epoch score) | Track points within `telemetry_keep_seconds` |
| `live:alerts:{flight_id}` | List (JSON) | Alert episodes generated by this flight |
| `live:active_alerts` | Set | Global set of active alert episode IDs |
| `live:alert_episodes` | Hash | Global episode index mapping episode ID to state JSON |
| `live:engine` | String | Engine heartbeat timestamp; expires via TTL if engine halts |
| `live:raw` | Pub/Sub channel | Stream of parsed raw receiver frames forwarded to `/ws/raw` |

**Historical flight storage (SQLite)**

When an aircraft exceeds `remember_planes` inactivity without new frames, the engine evaluates it for SQLite archival:

- A recorded alert episode under a `retain: true` rule endured for at least `dwell_seconds`.
- Retrospective analysis of the complete recorded track matches a `retain: true` rule for at least `dwell_seconds`.

Flights matching rules marked `retain: false` generate real-time alerts and populate live Redis keys, but are dropped from memory on expiry without committing to disk.

SQLite operates in Write-Ahead Logging mode (`PRAGMA journal_mode=WAL;`), allowing concurrent reads from the web portal while the tracking engine commits finalized flights. Relative database paths resolve against the directory containing the configuration file.

| Table | Indexed columns | Stored records |
|-------|-----------------|----------------|
| `flights` | `flight_id`, `icao`, `start_time`, `end_time` | Flight identifier, transponder code, callsign, and tracking bounds |
| `telemetry` | `flight_id`, `timestamp` | Time-series coordinate points and kinematic samples |
| `alerts` | `flight_id`, `zone`, `rule`, `activated_at` | Completed alert episodes with spatial boundary triggers |

Flight identifiers follow the compound format `{icao}-{first_packet_timestamp}` (such as `a1b2c3-1721832000`). Airframe metadata including operator, registration, model, and photo URLs is cached separately in `aircraft.db` through HexDB and Planespotters lookups, keeping historical flight tables decoupled from external API data.
