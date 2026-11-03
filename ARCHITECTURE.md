# Architecture

Internals for contributors. Users start at the [README](README.md).

**Processes**

- `pyaerial run` (engine, `engine/`) is the only writer of Redis and the SQLite archive. At start it claims the `live:engine` heartbeat; a second engine refuses to start, and an engine that loses the heartbeat yields.
- `pyaerial web` (portal, `webapp.py` and `api/`) reads Redis and opens the archive read-only. It never tracks.

**Pipeline**

`Engine.tick` (`engine/core.py`) runs `tracking.hz` times a second:

1. Drain the receiver intake queue and hold back copies that may still merge (`ReceiverSupervisor.drain`).
2. `IntegrityMonitor.observe_frames` sees every receiver copy, as does the raw frame archive (`archive.raw_frames`).
3. Merge copies, drop duplicates and CRC failures, publish to `live:raw`.
4. `Tracker.ingest` decodes and updates `tracker.planes`.
5. `IntegrityMonitor.evaluate` scores aircraft. `PlaneCalculator.calculate_all` (`pipeline.py`) runs kinematics, enrichment and the alert engine.
6. Publish the tracked flights to Redis (a cache for the portal) and refresh the heartbeat.
7. Expire idle aircraft, split long flights, retry deferred archive work and checkpoint live flights into the archive (`FlightArchiver`).

Each stage runs under a `StageGuard` (`engine/guard.py`, one instance shared by the engine, supervisor and archiver): an exception is logged (one traceback per stage per interval), counted in `counters.stage_errors`, and the tick continues.

**Layers**

Higher layers may import lower ones, never the reverse; `|` separates independent siblings. `lint-imports` enforces this from `[tool.importlinter]` in `pyproject.toml`:

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

**Engine package**

- `core.py`: `Engine`, the tick loop, stage guards, status line, heartbeat counters, signals, startup and shutdown order; `run_engine`.
- `supervisor.py`: `ReceiverSupervisor`, receiver threads (start, restart with back-off, stop), the bounded intake queue (sized from `tracking.hz`, drop-oldest), the merge-window hold-back, merge/de-dup, raw publish and raw frame archive.
- `archiver.py`: `FlightArchiver`, checkpoints, finalize, the archive policy (`retain`), deferred flights and their memory budget, retry back-off and the per-tick budget, startup recovery, segmentation (pure helpers in `segments.py`).
- `guard.py`: `StageGuard`. `lint-imports` keeps `core` above `supervisor | archiver` (independent of each other) above `segments | guard`. Both components take narrow protocols (`FrameArchive`, `RawPublisher`, `FrameDeduper`; `FlightArchive`, `LiveWriteStore`, `AlertHooks`, `EpisodeLog`), not the `Engine`.
- Test seams: `Engine.tick()`, `process_frames()`, `expire_planes()`, `log_status(now=)`, `request_stop()`, the `history_store=` argument (a real archive behind `isolated=True`); `supervisor.enqueue(frame)`, `drain()`, `held`, `receivers`, `queue`; `archiver.pending`, `checkpoint(now, force=)`, `defer()`, `clear_pending()`, `retry_now()`, `back_off()`, `max_flight_seconds`. Prefer them to private attributes.

**Threads**

- The tick loop (main thread) owns `tracker.planes`.
- Each receiver runs on its own thread and hands frames over through the bounded intake queue (`emit`); when it is full the oldest frame is dropped.
- Enrichment lookups run on `callsign-lookup-*` threads fed by a queue; results come back under a lock and are applied on a later tick.
- Alerter calls run on `AlertDispatcher` workers, each with a bounded queue. Jobs for one `alert_id` stay on one worker, so they arrive in order.
- Receiver-position polling and `cross_network` lookups have their own background threads.
- The portal is one asyncio loop. Blocking Redis and SQLite work goes to `asyncio.to_thread` or the RPC pool (`web.ws_request_workers`); the `live:raw` subscriber is a thread.

**Persistence and recovery**

The archive is the record; Redis is a cache of what the engine tracks.

- A tracked flight gets a `live` row with its first checkpoint (`FlightArchiver.checkpoint`, each flight every `CHECKPOINT_SECONDS` = 5 s, spread over the ticks): samples older than `SETTLE_SECONDS` = 10 s, and new integrity verdicts. Alert episodes are written at activation and deactivation (`store/recorder.py`, `EpisodeRecorder`: notify, then record), their while-active refreshes with the checkpoint.
- Finalize rewrites the whole flight as `completed`, or deletes it by the archive policy (`discard_flight`; an episode carried over a segment split moves back to the segment where it began). Failed writes wait in memory (`retry_pending`, 1 GiB budget, overflow counted as `pending_evicted`); refused episodes wait in a bounded outbox that is copied to Redis (`live:episode_outbox`) until the archive accepts them.
- Startup (`recover_on_startup`, retried by the tick while the archive is down): replay the episode outbox, close every open episode of another process with one `ended_by: restart` deactivate (`AlertEngine.close_restored`), keep each leftover `live` flight as `interrupted` or delete it by the policy, then clear the Redis cache.

The archive package `store/archive/`:

- `schema.py`: DDL, `SCHEMA_VERSION`, migrations and inspection helpers (functions over a `sqlite3` connection). An upgrade runs in one transaction.
- `writer.py`: `ArchiveWriter`, for the engine and `pyaerial reset`. Creates and upgrades the schema; checkpoints, episodes, finalize, discard and recovery reads; writes and prunes raw frames; reports `health()`.
- `reader.py`: `ArchiveReader`, for the portal. Opens the file `mode=ro`, never runs DDL, hides `live` flights and their episodes, and reports `missing`, `needs_upgrade`, `incompatible` or `down` in `state`.
- `rows.py` (row decoding) and `_conn.py` (connection, lock, reconnect backoff) are shared. Plane-to-row conversion is in `store/codec.py`.

The live store package `store/live/` (Redis keys in CONFIGURATION.md):

- `writer.py`: `LiveWriter`, for the engine (and `pyaerial reset` with `writer=False`, which never claims the heartbeat). Claims and refreshes `live:engine`, publishes the tracked flights, telemetry and episodes in one pipeline per tick (deleting flights no longer tracked; everything again after a failure or reconnect), keeps the episode outbox copy, publishes raw frames. It holds nothing recovery needs.
- `null.py`: `NullLiveWriter`, the isolated engine's (replay, profiling, tests): writes nothing.
- `reader.py`: `LiveReader`, for the portal. Read-only queries (`_queries.py`, shared with the writer), `engine_heartbeat()`, the `live:raw` subscription; raises `LiveUnavailable` while Redis is down.
- `keys.py` (key names, TTLs, heartbeat payload), `_conn.py` (client and reconnect backoff), `_reads.py` (Redis queries) and `_raw.py` (raw listeners and subscription) are shared. `lint-imports` keeps the portal's readers from importing any writer.
- `protocols.py`: `LiveReadStore` (api/), `EpisodeStore` (alerts), `LiveWriteStore` (engine).

**Adding a receiver or alerter**

- Receiver: subclass `pyaerial.receivers.Receiver` and implement `run()` (call `self.emit(...)` per frame, poll `should_stop()`, return a reason when exiting). Top-level `host` / `port` reach only `dump1090`, `readsb` and `aoa` (`ReceiverConfig.receiver_arguments`); other receivers read `options`.
- Alerter: subclass `pyaerial.alerters.Alerter` and implement `alert(meta, payload)`.
- Both declare `known_options: ClassVar[frozenset[str]]` (every `options` key they read; the loader rejects others with a "did you mean" hint) and may override `@classmethod validate_options(options, *, config_dir) -> list[str]`: plugin-specific checks returning error messages, no I/O beyond local files, may normalize `options` in place (`replay` resolves its path). A message starting with `options.<key>:` is reported against that key. A receiver that reads a Beast/AVR feed also implements `endpoint(options) -> ReceiverEndpoint` (host, port, wire format), used by the self-feed check and the portal.
- Registration is by entry point: groups `pyaerial.receivers` and `pyaerial.alerters` (the builtins are declared in `pyproject.toml`; if PyAerial's metadata is missing they are imported directly). A third-party distribution declares, e.g., `[project.entry-points."pyaerial.receivers"] mysdr = "mypkg.sdr:MySdrReceiver"`. Builtin names cannot be replaced (warning); a plugin that fails to import or lacks `known_options` is logged once and skipped. In-process code may use `@register_receiver("name")` / `@register_alerter("name")`.
- The loader (`config/loader.py`) only calls the contract; rules that need several sections (the self-feed Beast port check) stay there.
- The JSON webhook body (`alerters/contract.py`) and the WebSocket messages are public contracts pinned by golden files in `tests/golden/`.

**Tests and checks**

```bash
pip install -e ".[dev]"
pytest
pytest -m slow   # crash injection: SIGKILL loop against a real archive (~1 min)
ruff check src tests
lint-imports
cd web && npm ci && npm run lint && npm run typecheck && npm test
```

After an intended contract change, regenerate the golden files and review the diff:

```bash
PYAERIAL_UPDATE_GOLDEN=1 pytest tests/test_contract_webhook.py tests/test_contract_api.py
```
