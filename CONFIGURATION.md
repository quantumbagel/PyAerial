# Configuration

PyAerial reads one YAML file. Templates: [`config.yaml`](config.yaml) and [`src/pyaerial/examples/config.yaml`](src/pyaerial/examples/config.yaml). Validation is strict: unknown keys (also inside receiver and alerter `options`, with a "did you mean" hint), NaN or infinite numbers and out-of-range values fail at load. `pyaerial validate` runs the same checks without starting anything.

**Annotated example**

```yaml
database:
  path: pyaerial.db                 # SQLite research archive
  redis_uri: redis://localhost:6379/0

home:                               # station reference point (required)
  latitude: 35.727488
  longitude: -78.695942

receivers:                          # at least one
  main:
    type: dump1090
    host: localhost
    port: 30005                     # Beast: signal level + 12 MHz clock for the integrity checks
    # position: {latitude: 35.7275, longitude: -78.6959, altitude: 95}
  # array:
  #   type: aoa                     # angle-of-arrival sidecar
  #   host: localhost
  #   port: 30010
  # recorded:
  #   type: replay
  #   options: {path: captures/adsb.raw}

archive:
  flights: all                      # all | rules
  raw_frames: false                 # true: keep every receiver copy of every frame

zones:
  airport_approach:
    coordinates:                    # [latitude, longitude] ring; or file: zones/approach.geojson
      - [35.7288, -78.6954]
      - [35.7303, -78.6965]
      - [35.7304, -78.6992]
      - [35.7288, -78.6954]
    rules:
      - name: low_altitude_warning
        color: "#ef4444"
        when:                       # all must hold; needs eta, distance or proximity
          altitude: { max: 1000 }   # m
          eta: { max: 120 }         # s
        dwell_seconds: 60
        on_activate:
          - method: print
          - method: webhook
            options:
              url: "${file:/run/secrets/webhook_url}"   # or "${PYAERIAL_WEBHOOK_URL}"
              format: discord       # json | discord | slack
        on_deactivate:
          - method: print
        while_active:
          interval_seconds: 30
          actions:
            - method: print
```

Every other key is optional; the tables below give the defaults.

**Interpolation and overrides**

- `${VAR}` and `${VAR:-default}` expand environment variables in string values. An unset variable without a default fails validation.
- `${file:/path}` is replaced by the file's single line, verbatim. Relative paths resolve against the config file. Use it for secrets such as webhook URLs (e.g. Docker secrets under `/run/secrets/`). A missing, empty or multi-line file fails validation.
- The `PYAERIAL_*` variables in [CLI.md](CLI.md) replace values from the file and are not interpolated.

**Units**

Storage, rules, the WebSocket API and JSON webhooks use SI and WGS84 units. Only displays convert: the portal shows feet and knots beside metric values, and Discord and Slack messages use feet, knots and ft/min.

| Quantity | Unit | From ADS-B |
|----------|------|------------|
| `altitude` | m | Barometric altitude: ft x 0.3048 |
| `gnss_height` | m above the WGS84 ellipsoid | Geometric height (TC 20-22 positions): ft x 0.3048. Can differ from barometric altitude by tens to hundreds of metres |
| Horizontal speed | km/h | kt x 1.852 |
| Vertical speed | m/s | ft/min x 0.00508 |
| Heading | degrees clockwise from true north | Track angle |
| Position | decimal degrees (WGS84) | CPR-decoded latitude and longitude |
| `distance` | km | Geodesic distance to the nearest polygon edge |
| `proximity` | m | `distance` x 1000 |
| `eta` | s | Travel time along the projected track to the polygon boundary; `0` inside |

**`database`**

| Key | Default | Meaning |
|-----|---------|---------|
| `path` | `pyaerial.db` | SQLite archive. Relative paths resolve against the config file's directory |
| `redis_uri` | `redis://localhost:6379/0` | Redis live store (`redis://`, `rediss://` or `unix://`) |
| `redis_password_file` | unset | File holding the Redis password on one line (e.g. a Docker secret). Relative to the config file. Not combinable with a password in `redis_uri` |

**`tracking`**

| Key | Default | Meaning |
|-----|---------|---------|
| `hz` | `2` | Evaluation frequency. Also sizes the receiver intake queue; on overflow the oldest frames are dropped with a warning |
| `remember_planes` | `120` | Seconds without frames before a flight is finalized and archived |
| `duplicate_packet_merging` | `5` | Frame deduplication window (s) |
| `backdate_packets` | `10` | Track samples used to estimate velocity and turn rate |
| `use_kalman_eta` | `false` | Kalman-filter horizontal velocity |
| `curved_projection` | `false` | Project the track along the estimated turn rate |
| `telemetry_keep_seconds` | `600` | Live trail kept in Redis; `0` keeps the whole flight |
| `max_flight_seconds` | `14400` | Archive longer tracks as segments; `0` = no limit |
| `status_interval` | `10` | Seconds between console status lines |
| `status_message_top_planes` | `5` | Aircraft listed per status line; `-1` all, `0` none |
| `advanced_status` | `true` | Add callsign and per-message-type counts to status lines |

**`archive`**

| Key | Default | Meaning |
|-----|---------|---------|
| `flights` | `all` | `all`: keep every flight with at least `min_positions` position samples. `rules`: keep only flights a `retain: true` rule matched. Under both, a flight a `retain: true` rule alerted on is kept |
| `min_positions` | `2` | Minimum position samples under `flights: all` |
| `raw_frames` | `false` | Store every receiver copy of every frame in the `frames` table. A busy multi-receiver site writes several GB a day |
| `raw_frames_keep_hours` | `24` | Raw frame retention; `0` keeps them forever |

**`logging`, `home`, `alert_colors`**

| Key | Default | Meaning |
|-----|---------|---------|
| `logging.level` | `info` | `debug`, `info`, `warning` (or `warn`), `error`; case-insensitive |
| `logging.file` | unset | Also log to this file |
| `home.latitude`, `home.longitude` | required | CPR decode reference and antenna position for receivers without one. Not a geofence centre |
| `alert_colors` | `{}` | Rule name to hex color, for the map |

**Receivers (`receivers.<name>`)**

| Key | Default | Meaning |
|-----|---------|---------|
| `type` | required | `dump1090`, `readsb`, `aoa`, `replay`, or a plugin's type (`pyaerial validate` lists them) |
| `host`, `port` | | TCP source (`dump1090`, `readsb`, `aoa`) |
| `format` | from port | `dump1090` and `readsb`: `beast` / `binary` or `avr` / `raw`, case-insensitive; also accepted in `options`. Port 30002 implies AVR, any other Beast |
| `position` | unset | Fixed antenna position: `latitude`, `longitude`, `altitude` (m above mean sea level, default 0) |
| `position_url` | `http://<host>:8080/data/receiver.json` (`dump1090`; `readsb` below) | Where to fetch the antenna position when `position` is unset |
| `antenna_height` | `10` | Metres above ground, for the radio-horizon check |
| `options` | `{}` | Type-specific options (below) |

- Prefer Beast (30005). AVR text (30002) has no signal level or clock, so most integrity checks cannot run. A `format` that contradicts the port is rejected, and so is a `dump1090` or `readsb` receiver that reads this machine's `web.beast_port`.
- The antenna position comes from `position`, else the receiver's `receiver.json` (published by dump1090-fa and readsb; refreshed every 60 s, so moving receivers work), else `home`. A fetched position uses the file's altitude or 0 m, so set `position` when receivers sit at very different heights. Failed fetches back off up to 15 min; redirects are not followed.
- A receiver silent for 150 s is reconnected.
- Extended squitters that fail CRC and DF18 frames with non-ICAO addresses are ignored. A first airborne position (or the first after 180 s without one) needs an even/odd CPR pair within 10 s.

*readsb (`type: readsb`)*. [readsb](https://github.com/wiedehopf/readsb) serves dump1090's Beast (`--net-bo-port`, 30005) and AVR (`--net-ro-port`, 30002) outputs, so it takes the same keys. An external readsb (or the ultrafeeder container) is read the same way as the bundled dump1090.

```yaml
receivers:
  roof:
    type: readsb
    host: feeder.lan
    port: 30005
    # position_url: http://feeder.lan/tar1090/data/receiver.json
```

- Read the full Beast output, not BeastReduce (`--net-beast-reduce-out-port`, 30006 in ultrafeeder): it is rate-limited and deduplicated, so the integrity checks miss most frames. Port 30006 is rejected; set `options.allow_reduced: true` if yours serves full Beast.
- readsb only writes `receiver.json` to its `--write-json` directory. Without `position_url`, PyAerial tries tar1090's `http://<host>/tar1090/data/receiver.json`, then `http://<host>:8504/data/receiver.json`, then `http://<host>:8080/data/receiver.json` (ultrafeeder's example port), and keeps the first that has a location. It carries no altitude unless readsb gets one from gpsd. A `--json-location-accuracy` other than 2 rounds the location (a warning is logged), which is too coarse for TDOA: set `position`.
- Keep readsb's defaults: without `--net-verbatim` it forwards CRC-corrected frames as dump1090 does, so different receivers' copies of a transmission are identical and pair up for TDOA. Give each PyAerial receiver a readsb that decodes one radio: Beast inputs merged into its output (`--net-bi-port`, `beast_in` connectors) mix clocks and repeat frames, which the `timing` and `replay` checks read as spoofing.

| Option | Default | Meaning |
|--------|---------|---------|
| `allow_reduced` | `false` | Accept port 30006 (only when it serves full Beast) |

*Angle of arrival (`type: aoa`)*. Bearings need a coherent multi-channel SDR (KrakenSDR, bladeRF 2.0, USRP B210/N310) running its own DSP; dump1090 has no phase information. The `aoa` receiver reads one JSON object per line from that sidecar:

```json
{"hex": "8d4840d6202cc371c32ce0576098", "bearing": 213.4, "elevation": 4.1, "cfo": -18250.0, "rssi": -12.5, "clock": 123456789}
```

Only `hex` is required. `bearing`: degrees from true north; `elevation`: degrees above the horizon; `cfo`: carrier frequency offset, Hz; `rssi`: dBFS; `clock`: 12 MHz ticks below 2^48. A `time` field is ignored.

| Option | Default | Meaning |
|--------|---------|---------|
| `bearing_offset` | learned | Degrees added to every bearing (array orientation error). When unset, it is learned from 5+ aircraft that pass every other check; until then `aoa` reports nothing for this receiver |
| `array_axis` | unset | Linear (two-element) arrays: also accept bearings mirrored about this axis. Disables learning, so set `bearing_offset` too |

*Replay (`type: replay`)*

| Option | Default | Meaning |
|--------|---------|---------|
| `path` | required | Capture file, relative to the config file: dump1090 Beast binary, or text lines of bare hex, `*HEX;`, `@CLOCKHEX;` or `timestamp hex` |
| `speed` | `1.0` | Keep `1.0`: frames are stamped at replay time, so other speeds trip the kinematics and timing checks |
| `loop` | `true` | Repeat at end of file. Aircraft then jump back to their start, which the integrity checks flag. `false` idles after one pass |
| `interval` | `0.1` | Seconds between frames without a timestamp or clock |

[`src/pyaerial/examples/replay.yaml`](src/pyaerial/examples/replay.yaml) replays the bundled `captures/adsb.raw` (about 2.5 minutes of synthetic traffic).

**Zones (`zones.<name>`)**

| Key | Default | Meaning |
|-----|---------|---------|
| `coordinates` | | Closed ring of `[latitude, longitude]` pairs |
| `file` | | `.kml`, `.kmz` or `.geojson` file instead of `coordinates` (not both); relative to the config file |
| `color` | unset | Hex color on the map |
| `rules` | required | One or more rules; names must be unique within the zone |

A polygon needs 3+ distinct vertices, non-zero area and no self-intersecting edges; it may cross the antimeridian. Geofence files (longitude, latitude order) must be under 20 MB and hold exactly one polygon without holes. KML with `DOCTYPE` or `ENTITY` declarations is rejected.

**Rules (`zones.<name>.rules[]`)**

| Key | Default | Meaning |
|-----|---------|---------|
| `name` | required | Unique within the zone |
| `color` | unset | Hex color for matching aircraft on the map |
| `when` | required | Conditions (below); all must hold |
| `dwell_seconds` | required | Whole seconds the conditions must hold to activate, and to count for `retain` |
| `hysteresis_seconds` | `0` | When non-zero, replaces `dwell_seconds` as the activation hold and delays deactivation by the same time |
| `predict_seconds` | unset | Evaluate against the position dead-reckoned this far ahead |
| `retain` | `true` | Archive flights this rule alerted on or matched continuously for `dwell_seconds` (judged per sample, with the verdict at that time), even under `archive.flights: rules` or below `min_positions`. `false` only stops forcing archival |
| `on_activate`, `on_deactivate` | `[]` | Alerter actions |
| `while_active` | unset | `interval_seconds` (whole seconds, default `60`) and `actions`, repeated while active |

*Conditions (`when`)*. Each field takes `min` and/or `max` (or `minimum` / `maximum`); `min > max` is rejected except for `heading`. There is no implicit inside-polygon test: every rule needs `distance`, `proximity` or `eta`. Units: see above.

| Field (aliases) | Meaning |
|-----------------|---------|
| `altitude` (`alt`) | Barometric altitude. Aircraft that report none use `gnss_height` |
| `speed` (`horizontal_speed`) | Ground speed |
| `vertical_speed` (`vert_speed`) | Climb or descent rate |
| `latitude` (`lat`), `longitude` (`lon`, `long`) | Position (the predicted one with `predict_seconds`) |
| `heading` (`direction`) | Track; ranges may wrap through north, e.g. `{min: 350, max: 10}` |
| `distance` (`dist`), `proximity` | Distance to the nearest polygon edge |
| `eta` | Time to the polygon boundary; `0` inside |
| `integrity` | Integrity score, 0-1. Never matches while the verdict is `unknown`. Example: `integrity: {max: 0.5}` with `distance: {max: 50}` |

**Alerters**

`method: print` logs one line per notification with the event (`hook`) and `alert_id`. `method: webhook` options:

| Option | Default | Meaning |
|--------|---------|---------|
| `url` | required | `https://` endpoint (`http://` only for localhost). Treat it as a secret (`${file:...}`); logs and errors show only `scheme://host`, even at `debug` |
| `format` | `json` | `json` (body below), or human-readable `discord` / `slack` messages (case-insensitive) |
| `method` | `POST` | `POST` or `PUT` |
| `headers` | `{}` | Extra request headers |

Bad options fail `pyaerial validate`. Other `method` values come from installed alerter plugins; `pyaerial validate` lists every receiver and alerter and where it came from (`builtin` or the plugin's distribution). A plugin that fails to load is listed as unavailable and reported only if the config uses it. Delivery: 5 s timeout per attempt; up to 3 attempts with backoff on transport errors and `5xx`; `429` is retried after `Retry-After` (at most 30 s); other `4xx` and all `3xx` (redirects are not followed) fail. Notifications for one `alert_id` are delivered in order.

*JSON body (`format: json`)*. Schema `pyaerial.alert/1`; see Compatibility in the [README](README.md). Every field is always present; "or null" fields can be `null`. Times are unix seconds; other units follow the table above.

| Field | Type | Meaning |
|-------|------|---------|
| `schema` | string | `"pyaerial.alert/1"` |
| `event` | string | `activate`, `while_active` or `deactivate` |
| `alert_id` | string | Episode ID, shared by every notification of one episode; pair on it |
| `flight_id` | string | `{icao}-{first_seen_unix}`, the flight the aircraft is tracked as now |
| `icao` | string | 6 lowercase hex digits |
| `callsign` | string | From ADS-B, else the aircraft database; `""` if unknown |
| `zone`, `rule` | string | Names from the config |
| `type` | string | Same as `rule` (kept from 0.x) |
| `color` | string | Rule or zone color, `#rrggbb` or `#rgb` |
| `timestamp`, `activated_at` | number | When the notification was raised; when the episode activated |
| `deactivated_at` | number or null | When it ended; `null` unless `event` is `deactivate` |
| `ended_by` | string or null | `deactivate` only: `cleared` (rule no longer holds), `lost` (not heard for `remember_planes`), `shutdown` (engine stopping), `restart` (left active by an engine that stopped uncleanly; closed at the next start) |
| `eta` | number or null | Seconds to the zone boundary along the projected track; `0` inside; `null` if never or unknown |
| `reason` | object | `zones` (zone name to ETA or null, for every zone), `rule`, `hook` (same as `event`, kept from 0.x), `ended_by` (deactivate only) |
| `registration`, `manufacturer`, `manufacturer_name`, `model`, `aircraft_type`, `owner`, `operator_callsign`, `country`, `photo_url` | string or null | Aircraft database fields |
| `telemetry` | object | `latitude`, `longitude`, `altitude`, `altitude_source`, `speed`, `heading`, `vertical_speed`, each possibly `null`. `altitude` is barometric, else GNSS height (`altitude_source`: `baro` or `gnss`) |
| `tracker_links` | object | `flightaware`, `adsbexchange`, `radarbox` URLs; empty if `icao` is empty |
| `map_links` | object | `google_maps`, `openstreetmap` URLs; empty without a position |

Changes from 0.x: `schema` and `event` are new, every field is always present, and `icao` is lowercase. `type` and `reason.hook` remain for compatibility. Discord and Slack messages mark GNSS altitude "(GNSS)".

Every activation gets exactly one deactivation, except episodes whose rule was removed from the config before a restart: those close silently.

**`web`**

| Key | Default | Env override | Meaning |
|-----|---------|--------------|---------|
| `origins` | `[]` | `PYAERIAL_WEB_ORIGINS` | Extra browser origins for the WebSockets, exact `scheme://host[:port]`; `["*"]` allows any |
| `allow_localhost_origins` | `false` | | Also allow any `localhost`, `127.0.0.1` or `[::1]` origin (dev servers) |
| `allowed_hosts` | `[]` | `PYAERIAL_WEB_ALLOWED_HOSTS` | Accepted `Host` names, e.g. `["tracker.example", "*.lan"]`; others get HTTP 400, except `localhost` and `127.0.0.1`. Needed for same-origin WebSockets under a hostname. Add a LAN IP if you browse by IP |
| `trusted_proxies` | `[]` | | Proxy IPs or CIDRs whose `X-Forwarded-For` is trusted; empty trusts only `127.0.0.1` |
| `enable_docs` | `false` | | Serve `/docs`, `/redoc` and `/openapi.json` |
| `metrics` | `true` | | Serve Prometheus `/metrics` ([WEBSOCKET.md](WEBSOCKET.md)): traffic and error counters, receiver names, no hosts or secrets. Set `false` if people who can reach the portal should not see them |
| `beast_host` | `127.0.0.1` | `PYAERIAL_BEAST_HOST` | Beast TCP bind address; `0.0.0.0` for other hosts or containers |
| `beast_port` | `30105` | `PYAERIAL_BEAST_PORT` | Beast TCP port (not 30005, dump1090's); `null` (env: `off`) disables it, `/ws/beast` still serves |
| `beast_max_clients` | `16` | | Concurrent Beast TCP feeders |
| `beast_max_clients_per_ip` | `4` | | Concurrent Beast TCP feeders per IP |
| `ws_max_connections` | `256` | | Concurrent WebSocket connections, all endpoints |
| `ws_max_connections_per_ip` | `32` | | Concurrent WebSocket connections per client IP |
| `ws_max_inflight` | `2` | | Concurrent RPC requests per connection (1-16) |
| `ws_rate_limit` / `ws_rate_burst` | `50` / `100` | | RPC requests per second per connection, and burst |
| `ws_rate_limit_per_ip` / `ws_rate_burst_per_ip` | `200` / `400` | | The same, shared by one client IP's connections |
| `ws_request_workers` | `4` | | Threads serving RPC requests (1-64) |

[WEBSOCKET.md](WEBSOCKET.md) describes how clients experience these limits.

*Exposing the portal*. The portal has no authentication or TLS. Before exposing it beyond localhost:

1. Put a reverse proxy in front that terminates TLS and authenticates users.
2. Add its hostname to `allowed_hosts`.
3. List it in `trusted_proxies`, or all clients share its IP for the per-IP limits. Under Docker, a proxy on the host connects from the Compose network's gateway, not `127.0.0.1`: list that address or the subnet.
4. Change the port mappings in `docker-compose.yml` only deliberately (e.g. `"127.0.0.1:10090:10090"` to `"10090:10090"`).

The portal sends CSP, `nosniff`, `no-referrer` and `X-Frame-Options: DENY` headers, and passes only `https:` photo URLs to the browser.

**`enrichment` and outbound connections**

| Key | Default | Meaning |
|-----|---------|---------|
| `enrichment.enabled` | `true` | The engine looks up each new ICAO address on hexdb.io and Planespotters (registration, type, operator, photo) and caches it in `aircraft.db`. `false` uses the cache only |

Other outbound traffic:

- The portal (`pyaerial web`) ignores `enrichment.enabled` and still looks up uncached addresses while a browser is connected.
- The browser loads map style, tiles, glyphs and sprites from OpenFreeMap (`tiles.openfreemap.org`), with a plain background if it is unreachable, and Planespotters thumbnails when you open a flight. Fonts and scripts come from the portal.
- `integrity.cross_network` (off by default) sends ICAO addresses to the configured aggregator.

**`integrity`**

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | Run the spoofing checks |
| `ok_score` / `spoofed_score` | `0.8` / `0.5` | Score at or above `ok_score` is `ok`; below `spoofed_score` is `spoofed`; between is `suspect` |
| `weights` | `{}` | Per-check weight overrides, 0-1, e.g. `{timing: 0.2}` |
| `disabled_checks` | `[]` | Checks to skip, e.g. `[appearance]` |
| `window_seconds` | `120` | Evidence kept per aircraft |
| `horizon_margin` | `1.25` | Radio horizon multiplier (refraction, ducting) |
| `max_range_km` | `500` | Range limit for `radio_horizon` |
| `rssi_excess_db` | `15` | `rssi_range` fails when the signal is this much stronger than predicted |
| `aoa_tolerance_deg` | `15` | Median bearing error allowed |
| `tdoa_tolerance_m` | `3000` | Median TDOA path-difference residual allowed |
| `mode_s_altitude_tolerance_m` | `100` | Mode S vs. ADS-B altitude difference allowed |

`integrity.cross_network` (sends ICAO addresses out):

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `false` | Compare positions with a public aggregator |
| `url` | `https://api.adsb.lol/v2/hex/{icao}` | Must contain `{icao}`; readsb-style JSON response |
| `min_interval_seconds` | `2` | Minimum spacing between requests |
| `recheck_seconds` | `120` | Re-query an address after this long |
| `max_distance_km` | `10` | Fail when the aggregator's position is farther away |
| `timeout_seconds` | `5` | Deadline per request, body included |

Point `cross_network` at a network this station does not feed; otherwise its own data, spoofed or not, comes back. Failures back off up to 10 min, honouring `Retry-After`; redirects are not followed.

**Integrity output**

Each live aircraft has `info.integrity`:

- `status`: `ok`, `suspect`, `spoofed` or `unknown`.
- `score`: 0-1, or `null` while `unknown`.
- `checks`: every check that had evidence, with `status` (`pass`, `warn`, `fail`) and `detail`.
- `emitter`: when TDOA fails with 3+ receivers, the multilaterated transmitter (`latitude`, `longitude`, `offset_km` from the claimed position).
- `rebroadcast: true`: heard only through TIS-B / ADS-R, so checks that assume the aircraft is the transmitter are skipped.

Each failing check multiplies the score by `1 - weight` (half that for `warn`). Checks run every 5 s. The status stays `unknown` until the aircraft has been tracked 30 s with 3+ checks reporting (2 for TIS-B / ADS-R), but a `suspect` or `spoofed` score is published at once. A status worsens immediately and improves only after holding for 30 s. A change to `spoofed` is logged at WARNING.

| Check | Weight | Needs | Fails when |
|-------|--------|-------|------------|
| `aoa` | 0.7 | `aoa` receiver | Measured bearing or elevation disagrees with the claimed position |
| `tdoa` | 0.7 | 2+ Beast receivers | Arrival-time differences disagree with the claimed position. Clocks sync from trusted aircraft; no GPS clock needed |
| `mode_s_altitude` | 0.6 | Mode S replies (DF0/4/16/20) | Interrogation replies report a different altitude than ADS-B |
| `radio_horizon` | 0.6 | Altitude | A receiver hears it beyond `max_range_km` or the 4/3-earth horizon x `horizon_margin` |
| `duplicate_icao` | 0.5 | | Positions keep landing far off the track (two transmitters, one address) |
| `cross_network` | 0.5 | `cross_network.enabled` | The aggregator places the address more than `max_distance_km` away |
| `emitter_cluster` | 0.5 | Beast or `aoa` | Several aircraft share one carrier offset, one too-strong signal level, or one bearing |
| `rssi_range` | 0.4 | Beast, 8+ aircraft | Signal is `rssi_excess_db` stronger than the station's model predicts for the claimed range |
| `rssi_trend` | 0.4 | Beast | Signal stays flat while the claimed range changes 2.5x or more |
| `kinematics` | 0.4 | | Movement disagrees with reported speed, track or climb rate |
| `timing` | 0.4 | Beast clock | Position squitters are not randomly spaced 0.4-0.6 s apart, or stop alternating even/odd |
| `replay` | 0.4 | | The same airborne position frame arrives twice within 5 s while the aircraft moves |
| `identity` | 0.3 | | An N-number callsign or registration disagrees with the FAA-assigned address |
| `mode_s_corroboration` | 0.2 | Mode S replies | No replies for this address while at least half the other aircraft send them |
| `message_mix` | 0.2 | | Airborne positions without velocity or identification messages |
| `appearance` | 0.2 | | First seen less than 20 km away at altitude |

**Live state (Redis)**

These keys are internal (see Compatibility in the [README](README.md)). They are a cache of what the engine tracks, for the portal: the engine republishes them each tick (and in full after a Redis restart), deletes a flight's keys once it stops tracking it, and clears what a previous engine left at startup. Nothing in them is needed to recover after a crash, except `live:episode_outbox`. Per-flight keys also expire 7 days after their last write.

| Key | Type | Contents |
|-----|------|----------|
| `live:flights` | Set | Active flight IDs |
| `live:flight:{flight_id}` | String (JSON) | Latest state |
| `live:telemetry:{flight_id}` | Sorted set (epoch score) | Track points within `telemetry_keep_seconds` |
| `live:alerts:{flight_id}` | Hash (`alert_id` to JSON) | This flight's alert episodes |
| `live:active_alerts` | Hash (`alert_id` to JSON) | Active episodes, all flights |
| `live:alert_episodes` | Hash (`alert_id` to JSON) | All episodes |
| `live:engine` | String (JSON), 10 s TTL | Engine heartbeat, rewritten every tick (fields below) |
| `live:episode_outbox` | Hash (`alert_id` to JSON) | Alert episodes the archive refused (it was down), until it accepts them; the next engine replays them after a crash. Empty in normal operation |
| `live:raw` | Pub/Sub channel | Corrected receiver frames, served on `/ws/raw`, `/ws/beast` and Beast TCP |

`live:engine` fields:

| Field | Meaning |
|-------|---------|
| `seen_at`, `token` | Last write (unix seconds); the owning engine's ID |
| `history` | Archive health: `ok`, `down` or `disabled` |
| `history_error`, `history_pending` | Last archive error; flights waiting to be archived |
| `counters` | Since `counters.since` (engine start): `frames_received` (taken off the intake queue), `frames_dropped` (intake queue full), `messages_processed`, `pending_evicted` (deferred flights dropped from memory while the archive was down; their checkpointed part is kept as interrupted), `episodes_dropped` (alert episodes dropped from the unarchived outbox: it was full, or the archive rejected an entry 3 times, or a restored entry was unreadable; logged at ERROR), `receiver_restarts` (per receiver), `stage_errors` (per engine stage). Plus `planes_tracked`, the current count |

**Research archive (SQLite)**

Flight IDs are `{icao}-{first_packet_timestamp}`, e.g. `a1b2c3-1721832000`. `flights.status` is:

- `live`: being tracked. Every 5 s the engine checkpoints the flight's samples older than 10 s and its new integrity verdicts; alert episodes are written as they activate and close. Hidden from the portal's history.
- `completed`: finalized after `remember_planes` seconds without frames (or at shutdown, or at a segment boundary), and rewritten whole. A flight `archive.flights` does not keep is deleted instead, with its episodes.
- `interrupted`: the engine stopped without finalizing it (killed, crashed, power loss). At the next start it ends at its last checkpointed sample, so a crash loses at most about 15 s of each flight; its open alerts are closed with one deactivate (`ended_by: restart`, at their last refresh). The same `archive.flights` policy decides, on what was checkpointed, whether it is kept. The API shows it as `completed` with `interrupted: true`.

- Flights longer than `tracking.max_flight_seconds` become segments, each a flight of its own (`previous_flight_id` names the one it continues), sharing their boundary sample. Active alerts continue on the next segment under the same `alert_id`, without new notifications; the episode's row is on the segment where it ended (`start_flight_id` is where it began).
- While the archive is down, finished flights wait in memory (1 GiB) and episodes in an outbox (also copied to Redis), and are written once it is back. A crash during an outage loses the samples since it began.
- Suspected spoofs are archived like any other flight; filter on the provenance columns to select verified data.

The database runs in WAL mode; the portal opens it read-only. For backups, upgrades and schema compatibility see Operations in the [README](README.md) (current schema: 4, in `PRAGMA user_version`). A file from a newer PyAerial, or not a PyAerial archive, is refused and left untouched; the engine then runs with history offline, and `pyaerial reset --yes` recreates it.

| Table | Keys / indexes | Contents |
|-------|----------------|----------|
| `flights` | `flight_id`; `icao`, `callsign`, `end_time`, live flights | `status` (above), ICAO, callsign, registration, time bounds, final `info` document (with the last `integrity` verdict), `checkpoint_at`, `previous_flight_id`. `retained` is always 1 (legacy) |
| `telemetry` | `(flight_id, timestamp)`; `(icao, timestamp)` | Position samples: `altitude`, `gnss_height` (`NULL` except from TC 20-22 positions), speed, heading, provenance |
| `integrity` | `(flight_id, timestamp)`; `(icao, timestamp)` | Verdict log: every status change, plus score or check changes at most every 30 s. `score` (`NULL` for `unknown`), `status`, `flags` (JSON array of checks that did not pass), `checks` (full JSON) |
| `frames` | `timestamp`; `(icao, timestamp)` | With `raw_frames: true`, one row per receiver copy, before merging: `receiver`, `hex`, `df`, `icao` (DF17/18 only), `rssi`, `clock` (12 MHz ticks), `bearing`, `elevation`, `cfo` |
| `alerts` | `alert_id`; `flight_id`, `activated_at`, active episodes | Alert episodes, one row each, written from activation on; `reason` (JSON) has `ended_by` once closed; `start_flight_id` is the segment where it began |

Telemetry provenance columns:

| Column | Meaning |
|--------|---------|
| `receivers` | JSON array of receivers that heard the position frame; the kept (strongest) copy first |
| `integrity_score`, `integrity_status` | Verdict in effect when the sample arrived. `NULL` before the first verdict; `integrity_score` is also `NULL` while `unknown` |
| `integrity_flags` | JSON array of checks that did not pass in that verdict; `[]` when all passed |

```sql
-- Samples heard by two or more receivers while the aircraft verified as ok
SELECT * FROM telemetry
WHERE integrity_status = 'ok' AND json_array_length(receivers) >= 2;

-- Raw receiver copies behind one flight
SELECT f.* FROM frames f JOIN flights USING (icao)
WHERE flights.flight_id = 'a1b2c3-1721832000'
  AND f.timestamp BETWEEN flights.start_time AND flights.end_time;
```

Airframe metadata (operator, registration, model, photos) lives in the `aircraft.db` cache, not the archive.
