# Changelog

All notable changes to PyAerial are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- Receiver copies of one transmission were sometimes not merged: a copy stamped before it reached the engine's queue (dump1090 stamps are spread back from the TCP chunk's arrival) was released alone, and its partner from another receiver was then dropped as a duplicate, losing that receiver's provenance, its stronger signal on the Beast re-serve and its TDOA pairing. Frames are now held for the merge window after they reach the queue (at most 0.5 s), still merging by receive stamp.
- A Beast TCP feeder that stopped reading could keep its slot for minutes on Linux, where the kernel send buffer grows to several MiB before the 10 s drain timeout even starts. Accepted Beast sockets now use a 64 KiB send buffer.

## [1.0.0] - 2026-11-03

### Breaking / upgrade notes

Back up the archive and run `pyaerial validate` before upgrading (see "Upgrading" in the [README](README.md); 1.x stability promises are under "Compatibility" there).

- `web.beast_port` defaults to **30105** (was 30005, dump1090's own port). Point OpenSky and other feeders at 30105. A dump1090 receiver that reads the portal's Beast port on this machine is rejected.
- Stricter validation rejects: unknown keys in receiver and alerter `options`; an unknown dump1090 `format` or one that contradicts the port (`beast` on 30002, `avr` on 30005); duplicate rule names in a zone; degenerate or self-intersecting polygons; geofence files with holes, several polygons or bad vertices; `min > max` (except heading); NaN and infinite numbers; bad webhook `format` and `method`.
- An `integrity` rule condition never matches an `unknown` verdict (it saw a placeholder score of 1.0).
- `unknown` verdicts have `score: null` live, in `integrity.score` and in `telemetry.integrity_score`.
- `altitude` is barometric only. GNSS height is a separate `gnss_height`; rules and alerts use it only when an aircraft reports no barometric altitude.
- A first airborne position (or the first after 180 s without one) waits for an even/odd CPR pair, so tracks start a few seconds later but never at an aliased position.
- DF18 frames with non-ICAO addresses are no longer tracked.
- Archive schema 4. Archives from 0.12-0.22 are upgraded in place on the engine's first start; there is no downgrade.
- **Stop `pyaerial run` gracefully before upgrading** (`docker compose stop`, Ctrl-C, SIGTERM). A 0.x engine that crashed left its in-flight flights only in Redis; the new engine does not import them (it logs a warning with their count and clears them).
- The archive is now the source of truth for flights and alert episodes: a tracked flight is written as `live` from its first checkpoint, and a crash leaves it `interrupted` (ending at its last checkpoint) instead of being rebuilt from Redis. Under `archive.flights: rules`, an interrupted flight that no rule matches is deleted, where a crash leftover used to be kept.
- Docker Compose: the Redis password is a Compose secret read through `PYAERIAL_REDIS_PASSWORD_FILE`, no longer part of `PYAERIAL_REDIS`; a one-shot `redis-config` service renders the Redis config; the image ships only the example config, so bind-mount yours (Compose does). Recreate the stack after pulling.
- `fetchTelemetry` returns at most 5000 points by default (`limit` up to 20000), live and history, with `truncated` beside `data`.
- `pyaerial` without a subcommand exits 2 (was 0).
- JSON webhook bodies: `icao` is lowercase; `type` and `reason.hook` are kept for compatibility.

### Added

- `database.redis_password_file` / `PYAERIAL_REDIS_PASSWORD_FILE`.
- `${file:/path}` config interpolation for secrets.
- Versioned JSON webhook body: `"schema": "pyaerial.alert/1"`, a top-level `event`, and every field always present (field table in [CONFIGURATION.md](CONFIGURATION.md)).
- `tracking.max_flight_seconds` (default 14400): long tracks are archived as segments; active alerts carry over under the same `alert_id`.
- `enrichment.enabled`: turn off the engine's hexdb.io / Planespotters lookups.
- `readsb` receiver type: dump1090's Beast/AVR client plus readsb's tar1090 `receiver.json` locations; its BeastReduce port (30006) is rejected unless `options.allow_reduced: true`.
- Per-IP limits: `web.ws_rate_limit_per_ip` / `web.ws_rate_burst_per_ip` (200/s, burst 400) and `web.beast_max_clients_per_ip` (4).
- `integrity.cross_network.timeout_seconds`.
- `gnss_height` in the `telemetry` table and on WebSocket telemetry points.
- Alert notifications carry `alert_id`, `flight_id` and `altitude_source`; deactivations carry `reason.ended_by` (`cleared`, `lost`, `shutdown`, `restart`), also on WebSocket alerts and in the portal. `print` lines show the hook and `alert_id`.
- Archive health (`history`, `history_error`, `history_pending`) in the `live:engine` heartbeat.
- An archive lease (`engine_lease` table): a second engine waits out a running one, and an engine that stalled and was replaced can no longer write the archive.
- Archive checkpoints: every 5 s each tracked flight's samples (older than 10 s), new integrity verdicts and open alert episodes are written, so a crash loses at most about 15 s per flight (was everything older than the Redis telemetry window). New `flights.status` value `interrupted`; new columns `flights.checkpoint_at`, `flights.previous_flight_id`, `alerts.start_flight_id`. The API marks interrupted flights `interrupted: true` (status stays `completed`), and the portal's history shows a badge.
- `counters` in the `live:engine` heartbeat: frames received and dropped, messages processed, deferred flights and outbox episodes dropped, receiver restarts, stage errors, tracked aircraft.
- Replay of dump1090 Beast binary captures, and a bundled sample capture used by `examples/replay.yaml`.
- `pyaerial reset --force` for when Redis is unreachable.
- `subscribe` sends a snapshot for newly added `flights`, `alerts` and `stats` streams.
- Compose healthchecks for Redis, the engine and the portal, with startup ordering.
- `GET /metrics` in Prometheus text format: engine counters and health from the heartbeat, portal WebSocket and Beast clients, drops and rate limiting (`web.metrics: false` disables it).
- Third-party receivers and alerters through the `pyaerial.receivers` / `pyaerial.alerters` entry points; plugins declare `known_options` and `validate_options`, and `pyaerial validate` lists what is available.
- The portal's TypeScript types are generated from the server's wire models (`web/src/api/schema.json`), and CI fails on drift.
- This changelog.

### Changed

- Integrity verdicts stay `unknown` until an aircraft has been tracked 30 s with at least 3 checks reporting (2 for TIS-B/ADS-R); failures publish at once; a status improves only after holding 30 s. The `integrity` table logs score or check changes at most every 30 s.
- The AoA bias is learned from aircraft that pass every check except `aoa`; without `bearing_offset`, `aoa` stays silent until it is learned.
- `retain` needs a continuous match for `dwell_seconds` and uses the verdict in effect at each sample.
- Flight pushes also follow integrity changes.
- Older archives are upgraded in place in one transaction instead of being refused; an archive from a newer PyAerial is refused and left untouched.
- The portal opens the archive read-only and shows history as unavailable until the engine has created or upgraded it.
- Redis is a cache of what the engine tracks: it is republished every tick (in full after a Redis restart), cleared at startup, and no longer read to recover after a crash. Only alert episodes the archive refused during an outage are kept there (`live:episode_outbox`). Per-flight keys expire 7 days after their last write.
- The engine intake queue is sized from `tracking.hz`.
- `cross_network` and `receiver.json` requests have deadlines, back off on failure and honour `Retry-After`.
- `logging.level` is case-insensitive and accepts `warn`; errors from `PYAERIAL_*` overrides name the variable.
- The AoA sidecar's `time` field is ignored; frames use local receive time.
- `fetchConfig` no longer includes receiver hosts and ports.
- The logger `pyaerial.calc.plane` is now `pyaerial.pipeline`; update log filters.
- Compose: 20 s engine stop grace period, memory limits for Redis and the portal, log rotation, read-only Redis, Beast published on `127.0.0.1:${BEAST_PUBLISH_PORT:-30105}`.
- The web toolchain moves to Vite 8, Vitest 5, ESLint 10 and jsdom 30 on Node 22.23 (CI and the Docker build).
- The portal uses system fonts (no Google Fonts) and shows a plain map background when OpenFreeMap is unreachable.

### Removed

- UNITS.md; units are now a section of CONFIGURATION.md.

### Fixed

- Every alert activation gets one deactivation, also across crashes (`ended_by: restart`); a crash in the instant between a notification and its record can repeat one restart deactivate or leave one activation unpaired, and notifications still queued for delivery are lost with the process.
- Frames that fail CRC are no longer tracked or re-served on `/ws/raw`, `/ws/beast` or Beast TCP.
- A receiver clock jump (dump1090 restart, replay loop) resets that receiver's clock sync and `timing` check.
- Zones that cross the antimeridian.
- dump1090 and AoA receivers reconnect after 150 s of silence.
- A replay with `loop: false` no longer starts over when the engine restarts it.
- `radio_horizon` applies `max_range_km` to every receiver.
- Slow WebSocket clients and Beast TCP feeders that stop reading are disconnected instead of holding resources.
- Many robustness fixes in tracking, integrity, storage and the portal.

### Security

- The Redis password no longer appears in container environments or `docker inspect`.
- Webhook URLs never reach logs, including at `debug`.
- dump1090 runs with its network input ports disabled, so frames cannot be injected.
- The image no longer contains the local `config.yaml`; `.dockerignore` excludes it and `secrets/`.
- Outbound fetches (`receiver.json`, `cross_network`) do not follow redirects and bound response size and time.
- Per-IP WebSocket rate limits, a per-IP Beast TCP cap, and bounded send queues.

For changes before this release, see the [git tags](https://github.com/quantumbagel/PyAerial/tags) (`v0.1.0` to `v0.22.0`).

[Unreleased]: https://github.com/quantumbagel/PyAerial/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/quantumbagel/PyAerial/compare/v0.22.0...v1.0.0
