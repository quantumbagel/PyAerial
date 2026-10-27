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
| `receivers` | Named transport inputs (`dump1090`, `aoa`, `replay`) and where each antenna is |
| `zones` | Named polygonal geofences and associated rule arrays |
| `alert_colors` | Hex color overrides mapped to rule names for map rendering |
| `web` | Browser origin policy, WebSocket limits, Beast TCP bind and client cap, API docs toggle |
| `integrity` | Spoofing checks: weights, thresholds, opt-in public-network comparison |

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
#   trusted_proxies: []        # reverse proxy IPs/CIDRs whose X-Forwarded-For is trusted
#   ws_max_inflight: 2         # concurrent RPC requests per connection
#   ws_rate_limit: 50          # RPC requests/second per connection...
#   ws_rate_burst: 100         # ...with this burst allowance
#   ws_request_workers: 4      # threads serving RPC requests

# integrity:                   # all optional; defaults shown
#   enabled: true
#   ok_score: 0.8              # score >= ok_score: ok; < spoofed_score: spoofed
#   spoofed_score: 0.5
#   weights: {}                # per-check overrides, e.g. {timing: 0.2}
#   disabled_checks: []        # e.g. [appearance]
#   window_seconds: 120        # evidence kept per aircraft
#   horizon_margin: 1.25       # radio horizon multiplier (refraction, ducting)
#   max_range_km: 500
#   rssi_excess_db: 15         # signal this much stronger than predicted fails
#   aoa_tolerance_deg: 15
#   tdoa_tolerance_m: 3000
#   mode_s_altitude_tolerance_m: 100
#   cross_network:             # sends ICAO addresses to the URL below
#     enabled: false
#     url: "https://api.adsb.lol/v2/hex/{icao}"
#     min_interval_seconds: 2
#     recheck_seconds: 120
#     max_distance_km: 10

receivers:
  main:
    type: dump1090
    host: localhost
    port: 30005          # Beast binary (default): signal level + 12 MHz clock.
    # port: 30002        # AVR text (*8D...;): no signal level or clock, so most
    #                    # integrity checks cannot run.
    # position:          # Antenna location. Omit to read it from dump1090's
    #   latitude: 35.7275  # receiver.json, falling back to `home`.
    #   longitude: -78.6959
    #   altitude: 95      # metres above mean sea level
    # antenna_height: 10  # metres above ground (radio-horizon check)
  # array:
  #   type: aoa           # angle-of-arrival sidecar (see "Angle of arrival")
  #   host: localhost
  #   port: 30010
  #   options:
  #     bearing_offset: 0 # degrees added to every bearing; omit to learn it
  #     # array_axis: 90  # linear arrays: bearings are mirror-ambiguous about this axis
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
| `allowed_hosts` | `[]` | `PYAERIAL_WEB_ALLOWED_HOSTS` (comma-separated) | Accepted `Host` header values (`["tracker.example", "*.lan"]`); other hosts get HTTP 400, except `localhost` and `127.0.0.1`, which always stay reachable (healthchecks, local access). Add a LAN IP here too if you also browse by IP. Required for same-origin WebSockets when the portal is served under a hostname rather than an IP address or `localhost` |
| `enable_docs` | `false` | | Serve FastAPI's `/docs`, `/redoc` and `/openapi.json` |
| `beast_host` | `127.0.0.1` | `PYAERIAL_BEAST_HOST` | Beast TCP bind address. Use `0.0.0.0` for feeders on other hosts or in other containers |
| `beast_port` | `30005` | `PYAERIAL_BEAST_PORT` (`off` disables) | Beast TCP port; `null` disables TCP (`/ws/beast` still serves) |
| `beast_max_clients` | `16` | | Concurrent Beast TCP feeders; extra connections are closed |
| `ws_max_connections` | `256` | | Concurrent `/ws/live` + `/ws/beast` connections |
| `ws_max_connections_per_ip` | `32` | | Concurrent WebSocket connections per client IP. Behind a reverse proxy, list it in `trusted_proxies` or every client shares the proxy's IP |
| `trusted_proxies` | `[]` | | Proxy addresses (IPs or CIDRs) whose `X-Forwarded-For` gives the real client IP; empty trusts only `127.0.0.1` |
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
| `integrity` | 0-1 | Integrity score from the spoofing checks (`1` = consistent with every check). Combine with a spatial field, e.g. `integrity: {max: 0.5}` with `distance: {max: 50}` |

**Rule execution and retention controls**

| Parameter | Default | Function |
|-----------|---------|----------|
| `dwell_seconds` | Required | Continuous duration conditions must hold before triggering activation (when `hysteresis_seconds: 0`) and minimum match duration required to retain the flight |
| `retain` | `true` | When `false`, matching flights are excluded from SQLite archival regardless of dwell duration |
| `hysteresis_seconds` | `0` | When non-zero, sets both the activation hold time (overriding dwell) and the deactivation off-delay buffer |
| `predict_seconds` | Unset | Dead-reckons aircraft state forward by this duration and evaluates conditions against the projected position |

Polygon boundaries are declared as closed `[latitude, longitude]` coordinate rings or loaded from external geometries using `file: path` (`.kml`, `.kmz`, `.geojson`). Although standard GeoJSON and KML specifications store coordinates in `(longitude, latitude)` order, PyAerial normalizes all imported geometries to internal `[latitude, longitude]` pairs upon ingestion. Configurations must supply either `coordinates` or `file`, but not both.

**Receiver configurations**

TCP receivers connect to dump1090 using either `beast` binary encoding (port 30005, the default) or `avr` (port 30002). Beast input populates `rssi` and the 12 MHz sample clock that the integrity checks and the re-encoded `/ws/beast` / Beast TCP output use. Port 30002 selects AVR automatically unless overridden by explicit `format: beast`.

Every receiver has an antenna position, used as its CPR decode reference and for all geometry in the integrity checks. In priority order it comes from `position` in the config, from the receiver's own `receiver.json` (dump1090-fa and readsb publish their configured location at `http://<host>:8080/data/receiver.json`; override with `position_url`), or from `home`. Fetched positions refresh every 60 s, so a receiver on a vehicle keeps working. Positions from `receiver.json` carry no altitude, so set `position.altitude` when receivers sit at very different heights.

**Angle of arrival (`type: aoa`)**

dump1090 has no phase information, so bearings need a coherent multi-channel SDR (KrakenSDR, bladeRF 2.0, USRP B210/N310) running its own DSP. The `aoa` receiver connects to that sidecar over TCP and reads one JSON object per line:

```json
{"hex": "8d4840d6202cc371c32ce0576098", "bearing": 213.4, "elevation": 4.1, "cfo": -18250.0, "rssi": -12.5, "clock": 123456789}
```

`hex` is required; `bearing` is degrees clockwise from true north at the antenna, `elevation` degrees above the horizon, `cfo` the carrier frequency offset in Hz, `rssi` dBFS and `clock` 12 MHz ticks. Frames from the sidecar are tracked like any other receiver's. Set `options.bearing_offset` to the array's orientation error, or leave it out and PyAerial learns it from aircraft that pass the other checks. For a linear (two-element) array, set `options.array_axis` so bearings mirrored about that axis are accepted.

**Integrity (anti-spoofing)**

Each live aircraft gets `info.integrity`: a `score` from 0 to 1, a `status` (`ok`, `suspect`, `spoofed`, or `unknown` when no check has evidence yet), the result of every check that had evidence (`pass`, `warn` or `fail`, with a reason), and, for a target that fails TDOA with three or more receivers, the multilaterated `emitter` location. Each failing check multiplies the score by `1 - weight` (half that for `warn`). Checks are re-run every 5 s per aircraft; evidence accumulates continuously. A transition to `spoofed` is logged at WARNING with the failing checks.

Station-wide references are calibrated only on aircraft that currently score `ok`: the signal-strength model per receiver, AoA bias, and receiver clock offsets.

| Check | Needs | Fails when |
|-------|-------|------------|
| `aoa` | `aoa` receiver | Measured bearing (or elevation) disagrees with the bearing to the claimed position |
| `tdoa` | 2+ Beast receivers | Arrival-time differences between receivers disagree with the claimed position. Receiver clocks are synchronised continuously from trusted aircraft; no GPS clock is needed |
| `mode_s_altitude` | Mode S replies (DF0/4/16/20) | The aircraft's interrogation replies report a different altitude than its ADS-B |
| `radio_horizon` | Altitude | A receiver hears it from beyond line of sight (`max_range_km` or 4/3-earth horizon × `horizon_margin`) |
| `duplicate_icao` | | Positions for the address keep landing far off its track (two transmitters, one address) |
| `cross_network` | `cross_network.enabled` | A public aggregator places the address more than `max_distance_km` away |
| `rssi_range` | Beast, 8+ aircraft | Signal is `rssi_excess_db` stronger than the station's free-space model predicts for the claimed range (a nearby transmitter faking a distant target) |
| `rssi_trend` | Beast | Signal stays flat while the claimed range changes by 2.5x or more |
| `kinematics` | | Movement between positions disagrees with reported speed, track or climb rate |
| `timing` | Beast clock | Position squitters are not randomly spaced 0.4 to 0.6 s apart, or stop alternating even/odd |
| `replay` | | The same airborne position frame is received twice within 5 s while the aircraft moves |
| `emitter_cluster` | Beast or `aoa` | Several aircraft share one carrier offset, one signal level that is too strong, or one bearing |
| `mode_s_corroboration` | Mode S replies | No replies for this address while at least half the other aircraft send them (only applies under radar coverage) |
| `message_mix` | | Airborne positions without velocity or identification messages |
| `appearance` | | First seen less than 20 km away at altitude (aircraft normally enter at the edge of coverage) |
| `identity` | | An N-number callsign or registration disagrees with the FAA-assigned address |

`cross_network` sends ICAO addresses to the configured URL. If this station feeds that aggregator, its own data (spoofed or not) comes back, so point it at a network you do not feed.

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
