# Configuration Guide

PyAerial is configured through a single YAML file. Ready-to-use templates are available in [`config.yaml`](config.yaml) and [`src/pyaerial/examples/config.yaml`](src/pyaerial/examples/config.yaml).

Validation is strict by design: unrecognized keys (including inside receiver and alerter `options` blocks, where suggestions are provided for common typos), invalid numbers like NaN or infinity, and out-of-range numerical parameters will cause loading to fail immediately. You can run `pyaerial validate` at any time to verify your configuration file without starting background services.

## Annotated example

```yaml
database:
  path: pyaerial.db # SQLite research archive
  redis_uri: redis://localhost:6379/0

home: # station reference coordinates (required)
  latitude: 35.727488
  longitude: -78.695942

receivers: # at least one receiver must be defined
  main:
    type: dump1090
    host: localhost
    port: 30005 # Beast binary stream providing signal level and 12 MHz clock
    # position: {latitude: 35.7275, longitude: -78.6959, altitude: 95}
  # array:
  #   type: aoa                     # angle-of-arrival sidecar
  #   host: localhost
  #   port: 30010
  # recorded:
  #   type: replay
  #   options: {path: captures/adsb.raw}

archive:
  flights: all # choose between 'all' or 'rules'
  raw_frames: false # set true to save every receiver copy of every frame

zones:
  airport_approach:
    coordinates: # closed [latitude, longitude] ring, or file: zones/approach.geojson
      - [35.7288, -78.6954]
      - [35.7303, -78.6965]
      - [35.7304, -78.6992]
      - [35.7288, -78.6954]
    rules:
      - name: low_altitude_warning
        color: "#ef4444"
        when: # all conditions must match; requires eta, distance, or proximity
          altitude: { max: 1000 } # meters
          eta: { max: 120 } # seconds
        dwell_seconds: 60
        on_activate:
          - method: print
          - method: webhook
            options:
              url: "${file:/run/secrets/webhook_url}" # or "${PYAERIAL_WEBHOOK_URL}"
              format: discord # choose between json, discord, or slack
        on_deactivate:
          - method: print
        while_active:
          interval_seconds: 30
          actions:
            - method: print
```

All other configuration keys are optional and fall back to the defaults documented in the sections below.

## Variable interpolation and secrets

- String settings can reference environment variables using `${VAR}` or provide fallbacks using `${VAR:-default}`. If an environment variable is unset and provides no default value, validation fails.
- Secret values such as webhook endpoints or tokens can be read directly from files using `${file:/path}`. PyAerial reads the file, strips any trailing newline, and replaces the expression with the file's single line of text. Relative paths resolve against the directory containing your configuration file. Files that are missing, empty, or contain multiple lines will fail validation.
- The `PYAERIAL_*` environment variables documented in [CLI.md](CLI.md) override configuration file values directly without requiring interpolation syntax.

## Measurement units

Internally, PyAerial stores and processes all measurements in standard SI and WGS84 units across the database, geofence rules, the WebSocket API, and webhook payloads. Unit conversions happen only at the presentation layer: the web portal displays imperial units (feet, knots) alongside metric measurements, and Discord and Slack webhook notifications format altitudes in feet and climb rates in feet per minute.

| Quantity         | Base unit                         | Source from ADS-B                                                                                                |
| ---------------- | --------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `altitude`       | meters                            | Barometric altitude calculated as feet multiplied by 0.3048                                                      |
| `gnss_height`    | meters above the WGS84 ellipsoid  | Geometric height from TC 20–22 messages, which can differ from barometric altitude by tens to hundreds of meters |
| Horizontal speed | kilometers per hour               | Knots multiplied by 1.852                                                                                        |
| Vertical speed   | meters per second                 | Feet per minute multiplied by 0.00508                                                                            |
| Heading          | degrees clockwise from true north | Decoded track angle                                                                                              |
| Position         | decimal degrees (WGS84)           | CPR-decoded latitude and longitude                                                                               |
| `distance`       | kilometers                        | Geodesic distance to the nearest polygon boundary                                                                |
| `proximity`      | meters                            | Calculated as `distance` multiplied by 1000                                                                      |
| `eta`            | seconds                           | Estimated flight time to the polygon boundary along the projected path, or 0 when inside                         |

## Database configuration (`database`)

| Setting               | Default                    | Description                                                                                                                                                                            |
| --------------------- | -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `path`                | `pyaerial.db`              | Path to the SQLite research archive, resolved relative to the directory containing your configuration file                                                                             |
| `redis_uri`           | `redis://localhost:6379/0` | Connection URI for the Redis live store (`redis://`, `rediss://`, or `unix://`)                                                                                                        |
| `redis_password_file` | unset                      | Path to a single-line file containing the Redis password (such as a Docker secret), resolved relative to the configuration file; cannot be combined with a password inside `redis_uri` |

## Tracking loop settings (`tracking`)

| Setting                     | Default | Description                                                                                                                                      |
| --------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `hz`                        | `2`     | Evaluation frequency in Hertz; this also determines the intake queue capacity, and when the queue fills, older frames are dropped with a warning |
| `remember_planes`           | `120`   | Inactivity window in seconds after which an unobserved aircraft track is finalized and archived                                                  |
| `duplicate_packet_merging`  | `5`     | Time window in seconds used to deduplicate identical frames across stations                                                                      |
| `backdate_packets`          | `10`    | Number of recent track samples used to estimate velocity vectors and turn rates                                                                  |
| `use_kalman_eta`            | `false` | Enables Kalman filtering to smooth horizontal velocity estimates                                                                                 |
| `curved_projection`         | `false` | Projects trajectories along estimated turning arcs rather than straight lines                                                                    |
| `telemetry_keep_seconds`    | `600`   | Duration in seconds of recent track history preserved in Redis; set to `0` to keep the full flight path in cache                                 |
| `max_flight_seconds`        | `14400` | Maximum flight track duration before splitting into separate archive segments; set to `0` to disable segmentation                                |
| `status_interval`           | `10`    | Interval in seconds between periodic console status summaries                                                                                    |
| `status_message_top_planes` | `5`     | Number of prominent aircraft listed in each status summary; `-1` lists all aircraft, while `0` disables the list                                 |
| `advanced_status`           | `true`  | Appends callsign metadata and message-type breakdowns to console status lines                                                                    |

## Archival settings (`archive`)

| Setting                 | Default | Description                                                                                                                                                                    |
| ----------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `flights`               | `all`   | Controls flight archival; `all` saves every flight having at least `min_positions` position samples, while `rules` archives only flights matched by a rule with `retain: true` |
| `min_positions`         | `2`     | Minimum required position samples before a flight is written to SQLite under `flights: all`                                                                                    |
| `raw_frames`            | `false` | Stores every individual receiver frame copy in the `frames` database table; active multi-receiver installations can write several gigabytes daily                              |
| `raw_frames_keep_hours` | `24`    | Retention period in hours for raw frame records; setting to `0` keeps raw frames indefinitely                                                                                  |

## Station and logging settings

| Setting                           | Default  | Description                                                                                                                                                 |
| --------------------------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `logging.level`                   | `info`   | Logging verbosity (`debug`, `info`, `warn`, `warning`, or `error`), evaluated case-insensitively                                                            |
| `logging.file`                    | unset    | Optional filesystem path where logs should be duplicated                                                                                                    |
| `home.latitude`, `home.longitude` | required | Geographic reference coordinates for the station, used for local CPR decoding and as the fallback antenna location for receivers lacking explicit positions |
| `alert_colors`                    | `{}`     | Mapping of alert rule names to hex color codes for display on web maps                                                                                      |

## Receiver configuration (`receivers.<name>`)

| Setting          | Default           | Description                                                                                                                                       |
| ---------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `type`           | required          | Receiver driver type, including `dump1090`, `readsb`, `aoa`, `replay`, or custom plugin types (run `pyaerial validate` to list installed plugins) |
| `host`, `port`   |                   | Network address and port for TCP-based feeds (`dump1090`, `readsb`, and `aoa`)                                                                    |
| `format`         | derived from port | Wire format for `dump1090` and `readsb` (`beast` or `avr`); port 30002 defaults to AVR, while any other port defaults to Beast                    |
| `position`       | unset             | Fixed geographic coordinates for the receiver antenna: `latitude`, `longitude`, and `altitude` (meters above mean sea level, defaulting to 0)     |
| `position_url`   | auto-detected     | URL where receiver metadata can be queried if `position` is omitted                                                                               |
| `antenna_height` | `10`              | Receiver antenna height in meters above ground level, used for line-of-sight radio horizon calculations                                           |
| `options`        | `{}`              | Driver-specific configuration parameters                                                                                                          |

Whenever possible, connect to Beast binary feeds (typically on port 30005) rather than text-based AVR feeds (typically on port 30002). AVR streams omit signal levels and high-resolution timing clocks, which prevents the engine from running most integrity checks. The loader rejects configurations where `format` contradicts the specified port, and it prevents receivers from connecting back to PyAerial's own Beast server port.

The receiver's physical location is determined first by `position`, then by fetching `receiver.json` from the receiver (dump1090-fa and readsb refresh this file every 60 seconds, which allows mobile receiver setups to track their position), and finally falls back to `home`. If receivers in your setup are mounted at significantly different elevations, provide explicit `position` coordinates with accurate altitudes. HTTP queries back off for up to 15 minutes when a receiver is unreachable and will not follow redirects.

Receivers that remain silent for 150 seconds are automatically disconnected and reconnected. Extended squitter frames failing CRC checks and DF18 frames containing non-ICAO addresses are ignored. The first airborne position received for an aircraft (or the first position after a 180-second silence) requires an even and odd CPR frame pair within 10 seconds before track initialization.

### readsb receivers

[readsb](https://github.com/wiedehopf/readsb) exposes standard dump1090 Beast (`--net-bo-port`, 30005) and AVR (`--net-ro-port`, 30002) feeds, accepting the same parameters as dump1090:

```yaml
receivers:
  roof:
    type: readsb
    host: feeder.lan
    port: 30005
    # position_url: http://feeder.lan/tar1090/data/receiver.json
```

Always connect to full Beast streams rather than BeastReduce ports (`--net-beast-reduce-out-port`, port 30006 in ultrafeeder), because BeastReduce deduplicates and downsamples frames, which disrupts integrity verification. PyAerial rejects port 30006 by default unless `options.allow_reduced: true` is explicitly enabled.

If `position_url` is omitted, PyAerial probes tar1090 endpoints in order: `http://<host>/tar1090/data/receiver.json`, `http://<host>:8504/data/receiver.json`, and `http://<host>:8080/data/receiver.json`. If readsb is run with `--json-location-accuracy` set to anything other than 2, coordinates are rounded too coarsely for accurate TDOA multilateration, in which case explicit `position` coordinates should be provided.

Do not combine feeds from multiple SDRs into a single readsb instance before feeding PyAerial. Merged feeds combine independent receiver clocks and duplicate frames, which the `timing` and `replay` checks will misinterpret as active spoofing.

| Option          | Default | Description                                                                              |
| --------------- | ------- | ---------------------------------------------------------------------------------------- |
| `allow_reduced` | `false` | Permits connecting to port 30006 if that port is configured to emit unreduced Beast data |

### Angle of arrival receivers

Determining radio bearings requires a coherent multi-channel SDR (such as a KrakenSDR, bladeRF 2.0, or USRP) running dedicated direction-finding DSP software. The `aoa` driver reads newline-delimited JSON messages from the direction-finding sidecar:

```json
{
  "hex": "8d4840d6202cc371c32ce0576098",
  "bearing": 213.4,
  "elevation": 4.1,
  "cfo": -18250.0,
  "rssi": -12.5,
  "clock": 123456789
}
```

The `hex` field is required. Optional properties include `bearing` (degrees clockwise from true north), `elevation` (degrees above horizon), `cfo` (carrier frequency offset in Hertz), `rssi` (dBFS), and `clock` (12 MHz counter modulo 2^48). Any incoming `time` property is ignored in favor of local arrival timestamps.

| Option           | Default | Description                                                                                                                                                                                      |
| ---------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `bearing_offset` | learned | Constant angular offset added to bearings to correct antenna alignment errors; when omitted, PyAerial learns the bias automatically from five or more flights passing all other integrity checks |
| `array_axis`     | unset   | For two-element linear arrays with directional ambiguity, allows bearings mirrored across this axis; setting this disables automatic bias learning                                               |

### Replay receivers

The `replay` driver simulates live reception from prerecorded captures:

| Option     | Default  | Description                                                                                                                                                                           |
| ---------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `path`     | required | Path to the capture file, resolved relative to the configuration file; supports dump1090 Beast binary captures and raw hex text files                                                 |
| `speed`    | `1.0`    | Playback speed multiplier; leave this set to `1.0` because frames receive live timestamps upon playback, and altering playback speed will trip kinematics and timing integrity checks |
| `loop`     | `true`   | Restarts playback at end of file; aircraft will appear to jump across the map on loop reset, which triggers integrity alerts; set to `false` to idle after one playback pass          |
| `interval` | `0.1`    | Default delay in seconds between frames in captures lacking timing metadata                                                                                                           |

The bundled example in [`src/pyaerial/examples/replay.yaml`](src/pyaerial/examples/replay.yaml) replays synthetic traffic from `captures/adsb.raw`.

## Geofence zones (`zones.<name>`)

| Setting       | Default  | Description                                                                                                                                                       |
| ------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `coordinates` |          | Closed ring of `[latitude, longitude]` coordinate pairs defining the zone perimeter                                                                               |
| `file`        |          | Path to an external `.kml`, `.kmz`, or `.geojson` file defining the boundary (mutually exclusive with `coordinates`), resolved relative to the configuration file |
| `color`       | unset    | Hex color code used when rendering the zone perimeter on web maps                                                                                                 |
| `rules`       | required | List of one or more alert evaluation rules; rule names must be unique within each zone                                                                            |

Polygon boundaries require at least three distinct coordinate vertices, non-zero area, and no self-intersecting segments. Polygons may safely span across the antimeridian. External geofence files (which use longitude-latitude order) must remain under 20 MB and contain exactly one polygon without interior holes. KML files containing XML entity expansions or document type declarations are rejected for security.

## Alert rules (`zones.<name>.rules[]`)

| Setting                        | Default  | Description                                                                                                                                                                                 |
| ------------------------------ | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name`                         | required | Unique identifier for the rule within its parent zone                                                                                                                                       |
| `color`                        | unset    | Hex color code applied to matching aircraft on web maps                                                                                                                                     |
| `when`                         | required | Dictionary of conditions that must all evaluate to true for the rule to match                                                                                                               |
| `dwell_seconds`                | required | Consecutive seconds that conditions must hold before triggering an alert activation or counting toward retention                                                                            |
| `hysteresis_seconds`           | `0`      | When set above zero, replaces `dwell_seconds` for activations and delays alert deactivation by the same duration                                                                            |
| `predict_seconds`              | unset    | Projects aircraft trajectories this many seconds into the future before checking spatial conditions                                                                                         |
| `retain`                       | `true`   | Archives flights that triggered this rule or matched its conditions continuously for `dwell_seconds`, regardless of general retention thresholds; set to `false` to disable forced archival |
| `on_activate`, `on_deactivate` | `[]`     | List of actions dispatched when the rule activates or deactivates                                                                                                                           |
| `while_active`                 | unset    | Periodic action configuration containing `interval_seconds` (defaulting to 60) and `actions` executed while the alert remains active                                                        |

### Rule conditions

Every condition in the `when` block accepts `min` and `max` constraints (or `minimum` and `maximum`). Condition ranges where `min` exceeds `max` are rejected, with the exception of `heading`, which supports wrapping through north (for example `{min: 350, max: 10}`).

Geofences do not test boundary containment implicitly; rules must include at least one spatial condition such as `distance`, `proximity`, or `eta`.

| Field (aliases)                                 | Description                                                                                                                       |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `altitude` (`alt`)                              | Barometric altitude in meters, falling back to geometric height (`gnss_height`) for aircraft not broadcasting barometric readings |
| `speed` (`horizontal_speed`)                    | Ground speed in kilometers per hour                                                                                               |
| `vertical_speed` (`vert_speed`)                 | Rate of climb or descent in meters per second                                                                                     |
| `latitude` (`lat`), `longitude` (`lon`, `long`) | Aircraft geographic position (or projected position if `predict_seconds` is active)                                               |
| `heading` (`direction`)                         | Track heading in degrees clockwise from true north                                                                                |
| `distance` (`dist`), `proximity`                | Distance in kilometers (`distance`) or meters (`proximity`) to the nearest zone boundary                                          |
| `eta`                                           | Estimated flight time in seconds to the zone boundary, or 0 when inside                                                           |
| `integrity`                                     | Spoofing integrity score between 0.0 and 1.0; never matches while an aircraft's verdict is `unknown`                              |

## Alerters

PyAerial includes built-in alerter methods for terminal logging (`method: print`) and HTTP webhooks (`method: webhook`). The print method logs a single line containing the alert event (`hook`) and unique `alert_id`.

Webhook alerters accept the following parameters:

| Option    | Default  | Description                                                                                                                         |
| --------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `url`     | required | Destination HTTP endpoint (must use HTTPS unless targeting localhost); treat this value as sensitive and store it via `${file:...}` |
| `format`  | `json`   | Payload structure, supporting `json` (documented below), `discord`, or `slack`                                                      |
| `method`  | `POST`   | HTTP request method (`POST` or `PUT`)                                                                                               |
| `headers` | `{}`     | Additional HTTP headers sent with each notification                                                                                 |

Webhook requests use a 5-second connection timeout and retry up to three times with exponential backoff on network failures and HTTP 5xx responses. Rate-limited responses (HTTP 429) respect the `Retry-After` header up to a maximum delay of 30 seconds. Permanent client errors (HTTP 4xx) and redirects fail immediately without following redirect targets. All notifications for a given `alert_id` are delivered in strict sequential order.

### Webhook alert payloads

JSON webhooks conform to schema `pyaerial.alert/1`. All fields are present in every payload, with unavailable values set to `null`:

| Field                       | Type           | Description                                                                                                                                                        |
| --------------------------- | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `schema`                    | string         | Constant identifier `pyaerial.alert/1`                                                                                                                             |
| `event`                     | string         | Lifecycle transition: `activate`, `while_active`, or `deactivate`                                                                                                  |
| `alert_id`                  | string         | Unique episode identifier shared across all notifications for this alert                                                                                           |
| `flight_id`                 | string         | Track identifier formatted as `{icao}-{first_seen_unix}`                                                                                                           |
| `icao`                      | string         | Six-character lowercase hexadecimal ICAO transponder address                                                                                                       |
| `callsign`                  | string         | Flight callsign from ADS-B or the metadata database, or empty string if unknown                                                                                    |
| `zone`, `rule`              | string         | Names of the matching geofence zone and rule                                                                                                                       |
| `type`                      | string         | Identical to `rule` (retained for backward compatibility)                                                                                                          |
| `color`                     | string         | Configured hex color code for the rule or zone                                                                                                                     |
| `timestamp`, `activated_at` | number         | Unix timestamp when the notification was created, and when the alert first triggered                                                                               |
| `deactivated_at`            | number or null | Unix timestamp when the alert ended; `null` unless the event is `deactivate`                                                                                       |
| `ended_by`                  | string or null | Cause of deactivation: `cleared` (rule stopped matching), `lost` (aircraft vanished), `shutdown` (engine stopping), or `restart` (cleared during startup recovery) |
| `eta`                       | number or null | Seconds to the zone boundary along the current track, or 0 when inside                                                                                             |
| `reason`                    | object         | Context object containing `zones` (ETAs to each configured zone), `rule`, `hook` (legacy alias for `event`), and `ended_by`                                        |
| `telemetry`                 | object         | Latest kinematic readings including `latitude`, `longitude`, `altitude`, `altitude_source` (`baro` or `gnss`), `speed`, `heading`, and `vertical_speed`            |
| Metadata fields             | string or null | Aircraft registration, manufacturer, model, operator, and Planespotters photo URL                                                                                  |
| `tracker_links`             | object         | Direct links to FlightAware, ADS-B Exchange, and RadarBox                                                                                                          |
| `map_links`                 | object         | Direct links to Google Maps and OpenStreetMap at the current coordinates                                                                                           |

Every alert activation produces exactly one deactivation event across crashes and restarts (`ended_by: restart`), with the exception of alerts whose rules were removed from the configuration prior to restarting. Because the engine emits notifications before writing them to the database, a crash occurring precisely between transmission and logging may occasionally repeat a restart deactivation.

## Web service configuration (`web`)

| Setting                                         | Default       | Description                                                                                                       |
| ----------------------------------------------- | ------------- | ----------------------------------------------------------------------------------------------------------------- |
| `origins`                                       | `[]`          | Allowed browser origins for WebSocket handshakes (`scheme://host[:port]`), or `["*"]` to allow all                |
| `allow_localhost_origins`                       | `false`       | Permits connections from local origins (`localhost`, `127.0.0.1`, and `[::1]`)                                    |
| `allowed_hosts`                                 | `[]`          | Accepted HTTP Host header values; requests presenting unlisted hostnames receive HTTP 400                         |
| `trusted_proxies`                               | `[]`          | Trusted upstream reverse proxy IP addresses or CIDR blocks used to extract real client IPs from `X-Forwarded-For` |
| `enable_docs`                                   | `false`       | Serves interactive Swagger and ReDoc documentation at `/docs` and `/redoc`                                        |
| `metrics`                                       | `true`        | Enables the Prometheus metrics endpoint at `/metrics`                                                             |
| `beast_host`                                    | `127.0.0.1`   | Network interface address for the outgoing Beast TCP server                                                       |
| `beast_port`                                    | `30105`       | Port number for the Beast TCP server; set to empty or `null` to shut off TCP streaming                            |
| `beast_max_clients`                             | `16`          | Maximum total concurrent Beast TCP subscribers                                                                    |
| `beast_max_clients_per_ip`                      | `4`           | Maximum Beast TCP subscribers per client IP address                                                               |
| `ws_max_connections`                            | `256`         | Maximum concurrent WebSocket connections across all endpoints                                                     |
| `ws_max_connections_per_ip`                     | `32`          | Maximum concurrent WebSocket connections per client IP address                                                    |
| `ws_max_inflight`                               | `2`           | Maximum concurrent in-flight RPC requests allowed on a single connection                                          |
| `ws_rate_limit` / `ws_rate_burst`               | `50` / `100`  | RPC request rate limit and burst allowance per connection                                                         |
| `ws_rate_limit_per_ip` / `ws_rate_burst_per_ip` | `200` / `400` | Combined RPC rate limit and burst allowance per client IP address                                                 |
| `ws_request_workers`                            | `4`           | Thread pool size dedicated to handling RPC requests                                                               |

### Exposing the portal securely

The web portal includes no built-in user authentication or TLS encryption. Before making the service accessible across public or untrusted networks:

1. Deploy a reverse proxy in front of PyAerial (such as Nginx, Caddy, or Traefik) to terminate TLS and enforce user authentication.
2. Add your external domain name to `web.allowed_hosts`.
3. Add your proxy's internal IP address to `web.trusted_proxies` so per-IP rate limits inspect real client addresses. In Docker environments, host proxies connect through the bridge gateway address rather than `127.0.0.1`.
4. Ensure port bindings in `docker-compose.yml` bind only to intended interfaces.

The web service sets defensive security headers automatically, including Content Security Policy, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and `X-Frame-Options: DENY`.

## External lookups and metadata (`enrichment`)

| Setting              | Default | Description                                                                                                                                                                                             |
| -------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `enrichment.enabled` | `true`  | Enables background lookups against hexdb.io and Planespotters to retrieve airframe models, operators, and photos, caching results in `aircraft.db`; setting to `false` restricts lookups to local cache |

Additional outbound network requests include:

- When a user opens an aircraft in the web portal, the web application fetches Planespotters thumbnail images directly.
- The web browser downloads map tiles and vector glyphs from OpenFreeMap (`tiles.openfreemap.org`), falling back to a clean neutral background if external maps are unreachable.
- If `integrity.cross_network` is enabled, the engine queries configured public flight aggregators to verify broadcast positions.

## Spoofing detection and integrity (`integrity`)

| Setting                       | Default       | Description                                                                                                                                             |
| ----------------------------- | ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `enabled`                     | `true`        | Enables the spoofing detection engine                                                                                                                   |
| `ok_score` / `spoofed_score`  | `0.8` / `0.5` | Aircraft with scores at or above `ok_score` are marked `ok`, scores below `spoofed_score` are marked `spoofed`, and values between are marked `suspect` |
| `weights`                     | `{}`          | Custom weight overrides for individual checks, between 0.0 and 1.0                                                                                      |
| `disabled_checks`             | `[]`          | List of check names to exclude from evaluation                                                                                                          |
| `window_seconds`              | `120`         | Duration in seconds of historical evidence retained per aircraft                                                                                        |
| `horizon_margin`              | `1.25`        | Safety factor applied to line-of-sight calculations to account for atmospheric refraction                                                               |
| `max_range_km`                | `500`         | Absolute ceiling in kilometers for radio horizon plausibility checks                                                                                    |
| `rssi_excess_db`              | `15`          | Signal strength threshold above which an aircraft is deemed too strong for its reported distance                                                        |
| `aoa_tolerance_deg`           | `15`          | Maximum allowable angular error between measured and reported bearings                                                                                  |
| `tdoa_tolerance_m`            | `3000`        | Maximum allowable path-difference residual for TDOA multilateration                                                                                     |
| `mode_s_altitude_tolerance_m` | `100`         | Maximum allowable altitude discrepancy between Mode S replies and ADS-B broadcasts                                                                      |

### Cross-network verification (`integrity.cross_network`)

| Setting                | Default                              | Description                                                                            |
| ---------------------- | ------------------------------------ | -------------------------------------------------------------------------------------- |
| `enabled`              | `false`                              | Enables position verification against an external flight aggregator                    |
| `url`                  | `https://api.adsb.lol/v2/hex/{icao}` | Target API endpoint returning readsb-compatible JSON; `{icao}` is replaced dynamically |
| `min_interval_seconds` | `2`                                  | Minimum spacing between consecutive outbound API queries                               |
| `recheck_seconds`      | `120`                                | Interval before querying an aircraft's position again                                  |
| `max_distance_km`      | `10`                                 | Maximum allowed distance discrepancy before triggering a verification failure          |
| `timeout_seconds`      | `5`                                  | Request timeout per query including body download                                      |

Always point cross-network verification at an aggregator that your own receiver station does not feed. Otherwise, your station's own data—including any received spoofed frames—will be echoed back and appear to corroborate itself.

### Integrity checks and scoring

Live aircraft records include an `integrity` object detailing the active score:

- The overall `status` is classified as `ok`, `suspect`, `spoofed`, or `unknown`.
- The numeric `score` ranges from 0.0 to 1.0 and remains `null` while status is `unknown`.
- The `checks` mapping lists all evaluated checks alongside their results (`pass`, `warn`, or `fail`) and diagnostic details.
- When multilateration across three or more receivers confirms that an aircraft is transmitting from somewhere other than its reported location, `emitter` provides the transmitter's estimated coordinates and offset distance.
- When an aircraft is heard solely through ground rebroadcasts (TIS-B or ADS-R), `rebroadcast` is set to `true`, causing PyAerial to bypass checks that require direct line-of-sight transmission.

Each failing check reduces the score by multiplying it by `1 - weight` (or half that penalty for warnings). Checks run every five seconds. Aircraft remain in `unknown` status until they have been tracked for 30 seconds with at least three checks reporting (or two checks for rebroadcast aircraft), although suspect or spoofed scores publish immediately. A worsening status updates instantly, while an improving status requires 30 seconds of consistent evidence.

| Check                  | Weight | Hardware requirements           | Trigger condition                                                                                  |
| ---------------------- | ------ | ------------------------------- | -------------------------------------------------------------------------------------------------- |
| `aoa`                  | 0.7    | Angle-of-arrival array          | Measured bearing or elevation contradicts the reported position                                    |
| `tdoa`                 | 0.7    | Two or more Beast receivers     | Arrival time differences contradict reported positions; clocks synchronize using trusted flights   |
| `mode_s_altitude`      | 0.6    | Mode S replies (DF0, 4, 16, 20) | Interrogation replies report a different altitude than ADS-B messages                              |
| `radio_horizon`        | 0.6    | Aircraft altitude               | An aircraft is detected beyond `max_range_km` or past the atmospheric radio horizon                |
| `duplicate_icao`       | 0.5    |                                 | Sudden position jumps indicating multiple transmitters sharing one address                         |
| `cross_network`        | 0.5    | Cross-network enabled           | External aggregator places the aircraft farther than `max_distance_km` away                        |
| `emitter_cluster`      | 0.5    | Beast or AoA receivers          | Multiple aircraft share identical carrier offsets, suspicious signal levels, or identical bearings |
| `rssi_range`           | 0.4    | Beast receivers                 | Signal is significantly stronger than expected for the reported range                              |
| `rssi_trend`           | 0.4    | Beast receivers                 | Signal strength remains flat while range changes substantially                                     |
| `kinematics`           | 0.4    |                                 | Velocity, track, or vertical rate contradict physical trajectory progression                       |
| `timing`               | 0.4    | Beast receiver clock            | Messages fail to follow expected random jitter intervals or drop even/odd alternation              |
| `replay`               | 0.4    |                                 | Identical airborne position frames arrive twice within 5 seconds while moving                      |
| `identity`             | 0.3    |                                 | Reported N-number callsign or registration contradicts the ICAO address                            |
| `mode_s_corroboration` | 0.2    | Mode S replies                  | Aircraft emits no Mode S replies while surrounding traffic does                                    |
| `message_mix`          | 0.2    |                                 | Aircraft broadcasts airborne positions without accompanying velocity or identification messages    |
| `appearance`           | 0.2    |                                 | Aircraft first appears suddenly at high altitude within 20 km of the station                       |

## Redis live state schema

Redis functions as a transient live cache for the web portal. All keys are republished each tick and purged during clean engine startup. They are private to the PyAerial service and should not be relied upon by external integrations:

| Redis key                    | Data type       | Description                                                        |
| ---------------------------- | --------------- | ------------------------------------------------------------------ |
| `live:flights`               | Set             | Active flight identifiers                                          |
| `live:flight:{flight_id}`    | String (JSON)   | Most recent flight summary and metadata                            |
| `live:telemetry:{flight_id}` | Sorted set      | Recent trajectory points scored by epoch timestamp                 |
| `live:alerts:{flight_id}`    | Hash            | Alert episodes triggered by this flight                            |
| `live:active_alerts`         | Hash            | Currently active alert episodes across all flights                 |
| `live:alert_episodes`        | Hash            | Active and recently closed episodes for tracked aircraft           |
| `live:engine`                | String (JSON)   | Engine heartbeat updated every tick with a 10-second TTL           |
| `live:episode_outbox`        | Hash            | Alert episodes awaiting delivery to SQLite during database outages |
| `live:raw`                   | Pub/Sub channel | Stream of clean Mode S frames                                      |

The `live:engine` heartbeat payload tracks:

- `seen_at` and `token` recording the engine's last execution time and lease token
- `history` reporting database availability (`ok`, `down`, or `disabled`)
- `history_error` and `history_pending` logging recent database errors and pending write counts
- `counters` logging operational metrics since startup, including received frames, dropped frames, processed messages, evicted flights, receiver restarts, and stage errors

## SQLite research archive

Long-term flight trajectories and verification evidence are recorded in SQLite. The database operates in WAL mode, and the web portal accesses it read-only.

Flights are identified by `{icao}-{first_packet_timestamp}`. The database tracks lifecycle progress through the `status` column:

- Active flights are marked `live` while currently in the air. The engine checkpoints position samples and integrity verdicts every five seconds.
- Finalized flights are marked `completed` once the aircraft has departed or gone quiet, rewriting the complete trajectory and applying retention filters.
- Flights that were interrupted by a crash or sudden power cut are marked `interrupted`. When the engine restarts, it marks these flights as interrupted and preserves their track up to the last recorded checkpoint.

Tracks exceeding `tracking.max_flight_seconds` are divided into continuous segments, each assigned its own flight record referencing `previous_flight_id`. Suspicious and spoofed flights are preserved in the archive alongside legitimate traffic so researchers can study spoofing phenomena; verified data can be selected by querying provenance columns.

| Table          | Primary keys and indexes                                    | Description                                                                                          |
| -------------- | ----------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `flights`      | `flight_id`; indexed on `icao`, `callsign`, and `end_time`  | Flight metadata, start and end timestamps, final integrity verdict, and checkpoint status            |
| `telemetry`    | Indexed on `(flight_id, timestamp)` and `(icao, timestamp)` | Trajectory points including barometric altitude, geometric height, velocity, and receiver provenance |
| `integrity`    | Indexed on `(flight_id, timestamp)` and `(icao, timestamp)` | Chronological log of integrity verdicts, recording status changes and periodic score evaluations     |
| `frames`       | Indexed on `timestamp` and `(icao, timestamp)`              | Raw receiver frames recorded when `raw_frames: true` is enabled                                      |
| `alerts`       | `alert_id`; indexed on `flight_id` and `activated_at`       | Alert episodes recording activation details, deactivation reasons, and timing                        |
| `engine_lease` | Single row                                                  | Active engine lease token and renewal timestamp used for split-brain prevention                      |

Telemetry provenance columns allow filtering records according to verification confidence:

- `receivers`: JSON list of all stations that detected the frame, with the strongest receiver first
- `integrity_score` and `integrity_status`: Verdict in effect when the position frame arrived
- `integrity_flags`: List of specific checks that did not pass at the time of the sample

```sql
-- Query verified samples heard by two or more receivers
SELECT * FROM telemetry
WHERE integrity_status = 'ok' AND json_array_length(receivers) >= 2;

-- Query raw receiver copies recorded for a specific flight
SELECT f.* FROM frames f JOIN flights USING (icao)
WHERE flights.flight_id = 'a1b2c3-1721832000'
  AND f.timestamp BETWEEN flights.start_time AND flights.end_time;
```
