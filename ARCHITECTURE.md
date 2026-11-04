# Architecture

This document outlines the internal design, component structure, and operational guarantees of PyAerial for contributors and maintainers. If you are looking for configuration or user documentation, start with the [README](README.md).

## Core processes

PyAerial separates real-time sensor processing from client-facing presentation into two distinct services:

- `pyaerial run` represents the tracking engine implemented in `src/pyaerial/engine/`. It is the sole process that writes to Redis and the SQLite research database. During startup, the engine claims the `live:engine` heartbeat key and takes out an `engine_lease` in SQLite. If another engine instance is already active, a second engine will refuse to start; similarly, if a running engine ever loses its lease, it yields execution to protect data integrity.
- `pyaerial web` provides the web portal and client API, implemented across `webapp.py` and `api/`. It reads live aircraft snapshots from Redis and opens the SQLite archive in read-only mode. The web service never decodes radio frames, mutates tracking state, or triggers alert evaluations.

## Processing pipeline

The engine processes incoming radio traffic in discrete ticks orchestrated by `Engine.tick` in `engine/core.py`. The loop runs at the rate specified by `tracking.hz`, executing the following stages in sequence:

1. The receiver supervisor drains newly received frames from the intake queue while briefly buffering frames that may still have duplicate copies in transit. Every received frame is held for `MERGE_WINDOW` (100 ms) after entering the queue, up to a maximum hold of `MAX_HOLD` (500 ms) plus the duration of a single tick.
2. The integrity monitor observes all raw receiver copies through `IntegrityMonitor.observe_frames`, capturing signal metrics and arrival timestamps across every station before any deduplication occurs. If raw frame archival is enabled in the configuration, copies are recorded in the database as well.
3. Frames are deduplicated across stations, discarding corrupt frames that fail CRC checks, and surviving clean frames are published to the `live:raw` Redis pub/sub channel.
4. The tracker ingests clean frames, decodes Mode S and ADS-B payloads, and updates active aircraft state in `tracker.planes`.
5. The integrity engine evaluates available evidence to update each aircraft's spoofing score, while `PlaneCalculator.calculate_all` calculates velocity vectors, fetches cached enrichment data, evaluates geofence rules, and raises alerts.
6. Tracked aircraft summaries and active alerts are refreshed in Redis to serve connected web clients, and the engine heartbeat is renewed.
7. The engine cleans up stale aircraft that have not broadcast within `remember_planes`, divides excessively long flights into clean segments, retries any deferred database operations, and checkpoints live flight tracks into the SQLite archive.

Each processing stage runs inside a `StageGuard` boundary (`engine/guard.py`). If an unexpected exception occurs inside a stage, the guard catches the error, logs the traceback once per throttle interval, increments `counters.stage_errors`, and allows the rest of the tick loop to continue running uninterrupted.

## Architectural layers

To maintain clean separation of concerns and prevent circular dependencies, PyAerial organizes its packages into distinct layers. Components in higher layers may import from lower layers, but lower layers may never import upward. Independent sibling packages separated by `|` cannot import from one another. This hierarchy is strictly enforced by `lint-imports` during CI using settings in `pyproject.toml`:

```
pyaerial.cli
pyaerial.engine | pyaerial.webapp | pyaerial.view
pyaerial.api | pyaerial.pipeline
pyaerial.config.loader
pyaerial.alerts
pyaerial.alerters | pyaerial.store | pyaerial.enrich
pyaerial.calc
pyaerial.integrity
pyaerial.tracker
pyaerial.classify
pyaerial.receivers
pyaerial.config.schema | pyaerial.config.geofence_file
pyaerial.models | pyaerial.logging_setup
pyaerial.constants | pyaerial.units | pyaerial.jsonutil | pyaerial.urls | pyaerial.plugins
```

## The engine subsystem

The engine package (`src/pyaerial/engine/`) is structured into several focused modules:

- `core.py` houses the `Engine` class, driving the tick loop, supervising stage guards, printing terminal status summaries, maintaining heartbeat counters, handling Unix signals, and orchestrating clean startup and shutdown sequences.
- `supervisor.py` contains `ReceiverSupervisor`, which manages background receiver threads, applies exponential backoff when a connection drops, buffers frames across the merge window, coordinates deduplication, and feeds the raw Redis stream and raw frame archiver.
- `archiver.py` implements `FlightArchiver`, which controls periodic database checkpoints, finalizes completed flights, enforces retention policies, manages in-memory retry queues when SQLite is unreachable, and divides long tracks into segments using helpers from `segments.py`.
- `guard.py` defines `StageGuard`, a lightweight boundary that wraps individual tick stages, prevents unexpected crashes from halting the tracking loop, and surfaces error counts in Prometheus metrics.

For testing purposes, the engine exposes clean control points such as `Engine.tick()`, `process_frames()`, `expire_planes()`, and `request_stop()`. Similarly, the supervisor and archiver expose helper methods like `supervisor.enqueue(frame)`, `supervisor.drain()`, and `archiver.checkpoint(now, force=True)`. Tests should rely on these documented seams rather than accessing private class attributes.

## Concurrency and threads

PyAerial uses a combination of dedicated OS threads and asynchronous event loops:

- The main thread runs the tracking engine's tick loop and exclusively owns the `tracker.planes` dictionary, eliminating the need for complex locking during track evaluation.
- Every configured receiver runs in its own background thread, delivering incoming frames to the intake queue through `emit`. If the queue reaches capacity, the oldest frames are dropped immediately to keep processing latency low.
- Callsign and aircraft registration lookups run on background `callsign-lookup-*` threads fed by an internal queue. Completed lookups return their results behind a thread lock and are applied during subsequent engine ticks.
- Outbound alert notifications are dispatched using `AlertDispatcher` worker threads with bounded queues. All actions associated with a single `alert_id` are routed to the same worker thread, ensuring that activation, repeat, and deactivation messages always arrive in sequence.
- Periodic receiver position polling and external aggregator queries run in independent background threads.
- The web portal runs inside an asyncio event loop. Blocking filesystem and database operations are delegated to worker threads via `asyncio.to_thread` or the RPC thread pool (`web.ws_request_workers`), while an independent background thread listens to incoming frames on the `live:raw` Redis channel.

## Persistence and crash recovery

The research archive in SQLite serves as the permanent source of truth, while Redis functions as an ephemeral, read-heavy cache for web clients.

When an aircraft is detected, the archiver creates a new flight record marked as `live`. Every five seconds, the engine checkpoints samples older than ten seconds into SQLite along with newly finalized integrity verdicts. Alert episodes are written to the database when they activate and when they close, while interim updates sync during regular checkpoints.

When an aircraft stops transmitting for longer than `remember_planes`, the flight is marked `completed` and rewritten in full, or removed entirely if it fails retention criteria. If SQLite becomes temporarily unreachable, completed flights wait in an in-memory buffer capped at 1 GiB, and unwritten alert episodes are buffered in an outbox that mirrors into Redis (`live:episode_outbox`).

If the tracking engine stops abruptly due to a power loss or crash, the subsequent engine run recovers cleanly during startup:

- The new engine replays any uncommitted alert episodes discovered in the Redis outbox.
- It finds any open alert episodes that were left active by the previous process and cleanly deactivates them with an `ended_by: restart` event.
- Leftover flights marked as `live` from the previous run are converted to `interrupted`, terminating their tracks at their last recorded checkpoint. If a flight does not meet retention rules, it is deleted.
- The Redis live cache is purged so stale aircraft do not linger on web maps.

To prevent split-brain scenarios where two engine processes attempt to write to the same database simultaneously, the engine uses cooperative leasing. The active engine acquires a lease token in the `engine_lease` table and renews it on every tick. The lease expires after ten seconds, matching the Redis heartbeat timeout. A newly started engine waits for any existing lease to expire before taking over. Furthermore, every database write checks the active lease token, ensuring that an engine that stalled and resumed after its lease was revoked will fail immediately with `LeaseLost` rather than corrupting the archive.

## Archive and storage packages

Database operations are split between SQLite storage (`store/archive/`) and Redis state management (`store/live/`):

- `store/archive/schema.py` contains table DDL, current schema version constants, and migration helpers. All schema migrations run inside an isolated database transaction.
- `store/archive/writer.py` provides `ArchiveWriter`, used by both the engine and `pyaerial reset`. It manages table creation, migrations, flight checkpoints, episode writes, finalization, raw frame storage, and database health reporting.
- `store/archive/reader.py` provides `ArchiveReader` for the web portal. It opens the SQLite database in read-only mode (`mode=ro`), filters out active flights from public history views, and exposes current database availability states (`missing`, `needs_upgrade`, `incompatible`, or `down`).
- `store/live/writer.py` implements `LiveWriter`, which claims the engine heartbeat in Redis, serializes tracked aircraft and active alerts in single-tick batch pipelines, and publishes raw frames.
- `store/live/reader.py` implements `LiveReader`, exposing read-only query helpers for the web portal and tracking Redis connection health.

Architectural boundaries in `pyproject.toml` prevent the web portal and API layers from importing storage writer classes directly.

## Extending receivers and alerters

PyAerial supports third-party receiver and alerter plugins via standard Python entry points:

- To implement a custom receiver, subclass `pyaerial.receivers.Receiver` and implement its `run()` method. Your code should call `self.emit(frame)` whenever a new frame is received, monitor `self.should_stop()`, and return an exit reason upon shutdown. The built-in `host` and `port` configuration keys apply to `dump1090`, `readsb`, and `aoa` types, while custom receivers receive their configuration via the `options` mapping. If your receiver streams Beast or AVR binary data over a socket, implementing `endpoint(options)` allows PyAerial to run automatic loopback checks.
- To create a custom alerter, subclass `pyaerial.alerters.Alerter` and implement `alert(meta, payload)`.
- Plugins must declare `known_options: ClassVar[frozenset[str]]` containing all accepted configuration keys. The configuration loader automatically rejects unrecognized keys and offers suggestions for minor typos. You can optionally implement `@classmethod validate_options(options, *, config_dir)` to perform domain-specific configuration validation.
- Register plugins using entry point groups `pyaerial.receivers` and `pyaerial.alerters` in your package's `pyproject.toml`. PyAerial's built-in components are registered the same way. When testing or running embedded scripts, you can also register classes directly using `@register_receiver("name")` and `@register_alerter("name")`.
- The webhook JSON payload and WebSocket message formats are public contracts protected by golden tests in `tests/golden/`.

## Development and testing

To run the local test suite, linter, import checker, and frontend verification:

```bash
pip install -e ".[dev]"
pytest
pytest -m slow   # runs crash-injection tests against real SQLite archives (~1 min)
ruff check src tests
lint-imports
cd web && npm ci && npm run lint && npm run typecheck && npm test
```

If you make deliberate changes to public webhook schemas or API wire models, update the test golden files and carefully inspect the resulting diff:

```bash
PYAERIAL_UPDATE_GOLDEN=1 pytest tests/test_contract_webhook.py tests/test_contract_api.py
```
