# Changelog

All notable changes to PyAerial are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases adhere to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- Multiple copies of a single transmission received across different stations are now reliably merged. Previously, if a frame copy was timestamped before entering the engine's queue—which happens because dump1090 timestamps are spread back across the duration of a received TCP chunk—it could be dispatched on its own while matching frames arriving shortly after from other receivers were dropped as duplicates. This caused the engine to lose alternate receiver provenance, stronger signal readings, and TDOA pairing opportunities. Frames are now held in the queue for the merge window (up to 0.5 seconds) while still coalescing based on their physical receive timestamps.
- Sockets for connected Beast TCP feeders now use an explicit 64 KiB send buffer. On Linux systems, kernel send buffers could previously balloon to several megabytes before the 10-second drain timeout kicked in, allowing stalled feeder clients to tie up connections for minutes.

## [1.0.0] - 2026-11-03

### Breaking changes and upgrade notes

Before upgrading an existing deployment, back up your SQLite archive and run `pyaerial validate` against your configuration file. Stability commitments across the 1.x series are detailed in the Compatibility section of the [README](README.md).

- Outgoing Beast data on `web.beast_port` now defaults to port 30105 rather than 30005, which avoids conflicting with dump1090's own default Beast port. Reconfigure OpenSky feeders and external consumers to connect to port 30105. Receiver configurations attempting to read PyAerial's own Beast port on the local machine will now be rejected.
- Configuration validation has been tightened across all subsystems. The parser rejects unrecognized keys in receiver and alerter `options` blocks, flags format mismatches such as specifying `beast` on port 30002 or `avr` on port 30005, disallows duplicate rule names within a zone, and rejects degenerate polygons, self-intersecting boundaries, multi-polygon geofences, and polygons containing interior holes. Values where `min` exceeds `max` (except for wrap-around headings), NaN or infinite numbers, and unsupported webhook methods or formats will now fail validation on startup.
- Rule conditions that check `integrity` will no longer match aircraft whose integrity verdict is `unknown`. Previously, unverified aircraft were assigned a placeholder score of 1.0; they now report `score: null` in live feeds, in `integrity.score`, and across `telemetry.integrity_score`.
- Altitudes are now treated strictly as barometric measurements. Geometric height reported in ADS-B messages is stored separately as `gnss_height`, and alert rules evaluate geometric height only when an aircraft provides no barometric data.
- Initial airborne position tracking now waits for an even and odd CPR message pair, which prevents aircraft tracks from beginning at aliased positions across the globe.
- DF18 messages carrying non-ICAO transponder addresses are no longer ingested into tracking state.
- The research database advances to schema version 4. Existing archives from versions 0.12 through 0.22 are migrated in place automatically during the engine's initial startup. Database migrations roll forward only, so an older PyAerial version cannot read an upgraded database.
- Stop existing `pyaerial run` processes gracefully before upgrading (using `docker compose stop`, Ctrl-C, or SIGTERM). In previous 0.x releases, an ungraceful crash left active flights stranded in Redis. The 1.0.0 engine treats the SQLite database as the sole source of truth and purges leftover unfinalized flights found in Redis upon startup after logging a count warning.
- Active flights and alert episodes are now anchored in the SQLite archive from their first 5-second checkpoint. If the engine terminates abruptly, surviving flights are preserved as `interrupted` records ending at their last recorded checkpoint. Under `archive.flights: rules`, interrupted tracks that failed to trigger a retention rule are discarded cleanly.
- In Docker Compose setups, the Redis password is provided as a Docker secret via `PYAERIAL_REDIS_PASSWORD_FILE` rather than through the `PYAERIAL_REDIS` connection string. A one-shot `redis-config` container prepares the configuration file, and the PyAerial container ships only sample configs, requiring host configs to be bind-mounted into `/opt/PyAerial/config.yaml`.
- The `fetchTelemetry` WebSocket RPC now defaults to returning up to 5000 points (with an upper limit of 20000) and supplies a `truncated` boolean indicator alongside returned data.
- Invoking `pyaerial` without specifying a subcommand now exits with status code 2 instead of 0.
- Webhook JSON payloads now format `icao` addresses in lowercase, retaining legacy fields like `type` and `reason.hook` for backward compatibility.

### Added

- Added support for reading Redis credentials from a file using `database.redis_password_file` and the corresponding `PYAERIAL_REDIS_PASSWORD_FILE` environment variable.
- Added support for reading secret values directly from external files in YAML configurations via `${file:/path}`.
- Introduced versioned JSON webhook alert payloads under schema identifier `pyaerial.alert/1`, adding top-level `event` indicators and ensuring all fields are present consistently.
- Added `tracking.max_flight_seconds` (defaulting to 14400 seconds) to split exceptionally long flights into linked archive segments while maintaining active alert continuity under the same `alert_id`.
- Added the `enrichment.enabled` configuration toggle to allow disabling external hexdb.io and Planespotters API queries.
- Introduced a dedicated `readsb` receiver driver that consumes Beast or AVR streams while querying tar1090 `receiver.json` endpoints for dynamic antenna positions.
- Added configurable per-IP connection and rate limits for WebSockets and Beast TCP subscribers via `web.ws_rate_limit_per_ip`, `web.ws_rate_burst_per_ip`, and `web.beast_max_clients_per_ip`.
- Added `integrity.cross_network.timeout_seconds` to constrain external aggregator verification queries.
- Added `gnss_height` tracking across database tables and live WebSocket telemetry points.
- Enriched alert notifications with `alert_id`, `flight_id`, and `altitude_source` properties. Alert deactivations include an `ended_by` reason (`cleared`, `lost`, `shutdown`, or `restart`), which also surfaces across WebSocket feeds and in the web portal.
- Added archive health indicators (`history`, `history_error`, and `history_pending`) to the engine's live Redis heartbeat payload.
- Added database lease fencing via the `engine_lease` table to ensure that multiple engines cannot write to the same database simultaneously.
- Introduced periodic database checkpoints every 5 seconds for live flights, minimizing data loss during an unexpected power cut or crash to approximately 15 seconds per flight. Interrupted flights surface an `interrupted: true` flag in API queries and display a warning badge in web history.
- Added detailed operational counters to the engine heartbeat to track received frames, dropped frames, processed messages, evicted flights, receiver restarts, and stage errors.
- Added the ability to replay dump1090 Beast binary captures alongside synthetic sample data in `examples/replay.yaml`.
- Added the `--force` flag to `pyaerial reset` to allow purging SQLite data when Redis is unreachable.
- Subscribing to `flights`, `alerts`, or `stats` streams on an existing WebSocket connection now immediately pushes a fresh snapshot of current state.
- Added Docker Compose health checks and startup ordering dependencies for Redis, the engine, and the web portal.
- Added a Prometheus metrics endpoint at `GET /metrics` reporting real-time engine counters, heartbeat status, and web client statistics.
- Added dynamic plugin loading for third-party receivers and alerters via standard Python entry points (`pyaerial.receivers` and `pyaerial.alerters`).
- Generated TypeScript API types from server wire models to ensure the frontend build enforces strict contract compatibility in CI.

### Changed

- Aircraft integrity verdicts now remain in `unknown` status until an aircraft has been tracked for at least 30 seconds with three or more checks reporting (or two checks for rebroadcast data). Verification failures publish immediately, while improving verdicts require 30 seconds of consistent evidence.
- The angle-of-arrival bias is learned dynamically from aircraft that pass all other integrity checks. When `bearing_offset` is omitted, the AoA check remains silent until sufficient calibration data has been collected.
- Rules configured with `retain: true` now require conditions to hold continuously for `dwell_seconds`, evaluating the specific verdict in effect at each recorded sample.
- Flight updates are now pushed to web clients whenever integrity verdicts change.
- SQLite archives from previous releases are upgraded automatically in a single database transaction upon startup.
- The web portal opens the SQLite archive in read-only mode and displays history as unavailable until the engine finishes running migrations.
- Redis now acts strictly as an ephemeral cache of active flight state, republishing state each tick and clearing cached entries upon engine startup.
- The intake queue capacity scales automatically with `tracking.hz`.
- External HTTP queries for receiver metadata and cross-network position checks now enforce deadlines and handle HTTP 429 backoff according to `Retry-After` headers.
- The `logging.level` setting is evaluated case-insensitively and accepts `warn` as an alias for `warning`.
- The AoA sidecar driver now ignores incoming `time` fields, stamping frames with local arrival times instead.
- The `fetchConfig` WebSocket RPC no longer exposes receiver hostnames and ports to connected clients.
- The logger previously named `pyaerial.calc.plane` has been renamed to `pyaerial.pipeline`.
- Docker Compose includes a 20-second stop grace period for the engine, restricts Redis and portal containers to 1 GB of memory, and publishes Beast TCP on `127.0.0.1`.
- Upgraded the web development toolchain to TypeScript 6, Vite 8, Vitest 5, ESLint 10, and jsdom 30 running on Node 22.
- The web portal uses system fonts rather than loading Google Fonts externally and renders a neutral fallback map background when OpenFreeMap tiles cannot be reached.

### Removed

- Removed `UNITS.md`; unit conversion documentation has been incorporated directly into `CONFIGURATION.md`.

### Fixed

- Guaranteed that every alert activation receives a matching deactivation notification even across unexpected engine crashes (`ended_by: restart`).
- Frames failing CRC checks are no longer ingested into tracking state or redistributed across `/ws/raw`, `/ws/beast`, or Beast TCP feeds.
- Abrupt receiver clock jumps reset clock synchronization state and the corresponding timing check.
- Fixed geofence calculations for zones that cross the 180th meridian.
- dump1090 and AoA receiver drivers automatically reconnect after 150 seconds of silence.
- Prerecorded replays with `loop: false` no longer restart unexpectedly when the receiver supervisor cycles.
- Corrected line-of-sight radio horizon calculations to apply `max_range_km` across all configured receivers.
- Slow WebSocket clients and stalled Beast TCP readers are disconnected cleanly to prevent resource exhaustion.
- Resolved various race conditions and edge cases across tracking, integrity evaluation, storage, and web UI components.

### Security

- Removed Redis passwords from container environment variables and `docker inspect` outputs by migrating to Docker secrets.
- Webhook endpoints are stripped from application logs and debug messages.
- The bundled dump1090 service disables all network input sockets, preventing frame injection over the network.
- The production Docker image excludes local `config.yaml` and `secrets/` directories.
- Outbound HTTP requests for receiver locations and cross-network verification enforce strict payload size limits and refuse to follow redirects.
- Added per-IP connection caps, per-IP rate limiting, and bounded outbound buffer queues across all web feeds.

For details on changes prior to 1.0.0, refer to the [git release tags](https://github.com/quantumbagel/PyAerial/tags) from `v0.1.0` through `v0.22.0`.

[Unreleased]: https://github.com/quantumbagel/PyAerial/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/quantumbagel/PyAerial/compare/v0.22.0...v1.0.0
