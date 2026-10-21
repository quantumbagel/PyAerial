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
| `web` | Browser origin policy, WebSocket limits, Beast TCP bind and client cap, API docs toggle |

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

# web:                         # all optional; defaults shown
#   origins: []                # extra browser origins, exact scheme://host[:port]; ["*"] = any
#   allow_localhost_origins: false
#   allowed_hosts: []          # hostnames the portal is served on (DNS-rebinding guard)
#   enable_docs: false         # serve /docs, /redoc, /openapi.json
#   beast_host: "127.0.0.1"    # 0.0.0.0 to accept Beast feeders from other hosts
#   beast_port: 30005          # dump1090-compatible Beast TCP (OpenSky). null disables.
#   beast_max_clients: 16
#   ws_max_connections: 256    # /ws/live + /ws/beast, all clients
#   ws_max_connections_per_ip: 32
#   ws_max_inflight: 2         # concurrent RPC requests per connection
#   ws_rate_limit: 50          # RPC requests/second per connection...
#   ws_rate_burst: 100         # ...with this burst allowance
#   ws_request_workers: 4      # threads serving RPC requests

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
              format: discord            # json (default), discord, slack
              # method: POST             # POST (default) or PUT
        on_deactivate:
          - method: print
        while_active:
          interval_seconds: 30
          actions:
            - method: print
```

**Web portal (`web`)**

| Key | Default | Env override | Function |
|-----|---------|--------------|----------|
| `origins` | `[]` | `PYAERIAL_WEB_ORIGINS` (comma-separated) | Browser origins allowed on `/ws/live` and `/ws/beast` besides the portal's own (same-origin is allowed for IP-address and `localhost` hosts, and for hostnames listed in `allowed_hosts`). Entries must be exact `scheme://host[:port]`; bare hostnames are rejected at load time. `["*"]` allows any origin |
| `allow_localhost_origins` | `false` | | Also allow any `http(s)://localhost`, `127.0.0.1` or `[::1]` origin, for dev servers on other ports |
| `allowed_hosts` | `[]` | | Accepted `Host` header values (`["tracker.example", "*.lan"]`); other hosts get HTTP 400. Required for same-origin WebSockets when the portal is served under a hostname rather than an IP address or `localhost` |
| `enable_docs` | `false` | | Serve FastAPI's `/docs`, `/redoc` and `/openapi.json` |
| `beast_host` | `127.0.0.1` | `PYAERIAL_BEAST_HOST` | Beast TCP bind address. Use `0.0.0.0` for feeders on other hosts or in other containers |
| `beast_port` | `30005` | `PYAERIAL_BEAST_PORT` (`off` disables) | Beast TCP port; `null` disables TCP (`/ws/beast` still serves) |
| `beast_max_clients` | `16` | | Concurrent Beast TCP feeders; extra connections are closed |
| `ws_max_connections` | `256` | | Concurrent `/ws/live` + `/ws/beast` connections |
| `ws_max_connections_per_ip` | `32` | | Concurrent WebSocket connections per client IP (behind a reverse proxy every client shares the proxy's IP) |
| `ws_max_inflight` | `2` | | RPC requests processed concurrently per connection (1 to 16) |
| `ws_rate_limit` / `ws_rate_burst` | `50` / `100` | | Token-bucket RPC rate per connection. Requests over the rate are delayed (the server stops reading the socket); a request that would wait more than 1 s gets `code: "rate_limited"` |
| `ws_request_workers` | `4` | | Threads in the dedicated RPC worker pool |

The portal also sends `Content-Security-Policy`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and `X-Frame-Options: DENY` on every HTTP response, caps inbound WebSocket frames at 64 KiB, and only passes `https:` aircraft photo URLs to the browser. See [WEBSOCKET.md](WEBSOCKET.md) for the limits as clients see them.

**Webhook alerter (`method: webhook`)**

| Option | Default | Function |
|--------|---------|----------|
| `url` | Required | `https://` endpoint (`http://` only for localhost). Treat it as a secret: logs and config errors show only `scheme://host` |
| `format` | `json` | `json`, `discord`, or `slack` body |
| `method` | `POST` | `POST` or `PUT` |
| `headers` | `{}` | Extra request headers |

Delivery never follows redirects (a `3xx` is a failure). Transport errors and `5xx` are retried up to 3 attempts with backoff; `429` is retried after its `Retry-After` (capped at 30 s); other `4xx` responses are not retried.

**Geofence files**

`.kml`, `.kmz` and `.geojson` files (and the KML inside a KMZ) are limited to 20 MB. KML containing `DOCTYPE` or `ENTITY` declarations is rejected.

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

TCP receivers connect to dump1090 using either `avr` (port 30002) or `beast` binary encoding (port 30005). Beast input populates `rssi` and the 12 MHz sample clock used when PyAerial re-encodes corrected frames for `/ws/beast` and the optional Beast TCP output. Configuring port 30005 selects Beast format automatically unless overridden by explicit `format: avr` settings.

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
| `live:raw` | Pub/Sub channel | Corrected receiver frames; the web process re-encodes them as Beast for `/ws/beast` and optional TCP |

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
