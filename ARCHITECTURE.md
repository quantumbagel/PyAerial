# Architecture

Internals for contributors. Users start at the [README](README.md).

**Processes**

- `pyaerial run` (engine, `engine.py`) is the only writer of Redis and the SQLite archive. At start it claims the `live:engine` heartbeat; a second engine refuses to start, and an engine that loses the heartbeat yields.
- `pyaerial web` (portal, `webapp.py` and `api/`) reads Redis and opens the archive read-only. It never tracks.

**Pipeline**

`Engine._tick` runs `tracking.hz` times a second:

1. Drain the receiver intake queue and hold back copies that may still merge.
2. `IntegrityMonitor.observe_frames` sees every receiver copy, as does the raw frame archive (`archive.raw_frames`).
3. Merge copies, drop duplicates and CRC failures, publish to `live:raw`.
4. `Tracker.ingest` decodes and updates `tracker.planes`.
5. `IntegrityMonitor.evaluate` scores aircraft. `PlaneCalculator.calculate_all` (`pipeline.py`) runs kinematics, enrichment and the alert engine.
6. Write live state and the heartbeat to Redis.
7. Expire idle aircraft, split long flights, retry deferred archive work.

Each stage runs under `Engine._guard`: an exception is logged (one traceback per stage per interval), counted in `counters.stage_errors`, and the tick continues.

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

**Threads**

- The tick loop (main thread) owns `tracker.planes`.
- Each receiver runs on its own thread and hands frames over through the bounded intake queue (`emit`); when it is full the oldest frame is dropped.
- Enrichment lookups run on `callsign-lookup-*` threads fed by a queue; results come back under a lock and are applied on a later tick.
- Alerter calls run on `AlertDispatcher` workers, each with a bounded queue. Jobs for one `alert_id` stay on one worker, so they arrive in order.
- Receiver-position polling and `cross_network` lookups have their own background threads.
- The portal is one asyncio loop. Blocking Redis and SQLite work goes to `asyncio.to_thread` or the RPC pool (`web.ws_request_workers`); the `live:raw` subscriber is a thread.

**Persistence and recovery**

A finished flight is written to the archive before its Redis copy is popped (`_finalize_plane`, then `_archive_and_pop`), so a SQLite outage cannot lose it. Recovery paths in `engine.py`:

- `_retry_pending_finalizes`: flights that failed to archive wait in memory (bounded; overflow counted as `pending_evicted`) and are retried with backoff.
- `_archive_startup_live_flights`: archives live flights a previous engine left in Redis.
- `_close_stray_active_alerts`: closes active alerts whose flight never reached Redis (`ended_by: restart`).
- `_retry_orphan_live_flights`: each tick, archives Redis flights nobody tracks.
- `LiveWriter.retry_pending_pops`: retries failed Redis deletes.

The archive package `store/archive/`:

- `schema.py`: DDL, `SCHEMA_VERSION`, migrations and inspection helpers (functions over a `sqlite3` connection). An upgrade runs in one transaction.
- `writer.py`: `ArchiveWriter`, for the engine and `pyaerial reset`. Creates and upgrades the schema, finalizes flights (a re-archive wins only with a longer time span, decided under `BEGIN IMMEDIATE`), writes and prunes raw frames, reports `health()`.
- `reader.py`: `ArchiveReader`, for the portal. Opens the file `mode=ro`, never runs DDL, and reports `missing`, `needs_upgrade`, `incompatible` or `down` in `state`.
- `_conn.py`: the connection, lock and reconnect backoff they share. Plane-to-row conversion is in `store/codec.py`.

The live store package `store/live/` (Redis keys in CONFIGURATION.md):

- `writer.py`: `LiveWriter`, for the engine (and `pyaerial reset` with `writer=False`, which never claims the heartbeat). Claims and refreshes `live:engine`, writes flights and telemetry in one pipeline per tick, publishes raw frames, and mirrors everything in memory (`store/memory.py`), replayed into Redis on reconnect. Alert episodes are in `episodes.py`, pops and finalize/recovery reads in `recovery.py`, the mirror and its fallback reads in `_mirror.py`. `memory_only=True` is the isolated mode: the mirror is the whole store.
- `reader.py`: `LiveReader`, for the portal. Read-only queries, `engine_heartbeat()`, the `live:raw` subscription; no memory mirror, so it raises `LiveUnavailable` while Redis is down.
- `keys.py` (key names, TTLs, heartbeat payload), `_conn.py` (client and reconnect backoff), `_reads.py` (Redis queries) and `_raw.py` (raw listeners and subscription) are shared. `lint-imports` keeps the reader from importing the writer side.
- `protocols.py`: `LiveReadStore` (api/), `EpisodeStore` (alerts), `LiveWriteStore` (engine).

**Adding a receiver or alerter**

- Receiver: subclass `pyaerial.receivers.Receiver` and implement `run()` (call `self.emit(...)` per frame, poll `should_stop()`, return a reason when exiting). Top-level `host` / `port` reach only `dump1090` and `aoa` (`ReceiverConfig.receiver_arguments`); other receivers read `options`.
- Alerter: subclass `pyaerial.alerters.Alerter` and implement `alert(meta, payload)`.
- Both declare `known_options: ClassVar[frozenset[str]]` (every `options` key they read; the loader rejects others with a "did you mean" hint) and may override `@classmethod validate_options(options, *, config_dir) -> list[str]`: plugin-specific checks returning error messages, no I/O beyond local files, may normalize `options` in place (`replay` resolves its path). A message starting with `options.<key>:` is reported against that key. A receiver that reads a Beast/AVR feed also implements `endpoint(options) -> ReceiverEndpoint` (host, port, wire format), used by the self-feed check and the portal.
- Registration is by entry point: groups `pyaerial.receivers` and `pyaerial.alerters` (the builtins are declared in `pyproject.toml`; if PyAerial's metadata is missing they are imported directly). A third-party distribution declares, e.g., `[project.entry-points."pyaerial.receivers"] mysdr = "mypkg.sdr:MySdrReceiver"`. Builtin names cannot be replaced (warning); a plugin that fails to import or lacks `known_options` is logged once and skipped. In-process code may use `@register_receiver("name")` / `@register_alerter("name")`.
- The loader (`config/loader.py`) only calls the contract; rules that need several sections (the self-feed Beast port check) stay there.
- The JSON webhook body (`alerters/contract.py`) and the WebSocket messages are public contracts pinned by golden files in `tests/golden/`.

**Tests and checks**

```bash
pip install -e ".[dev]"
pytest
ruff check src tests
lint-imports
cd web && npm ci && npm run lint && npm run typecheck && npm test
```

After an intended contract change, regenerate the golden files and review the diff:

```bash
PYAERIAL_UPDATE_GOLDEN=1 pytest tests/test_contract_webhook.py tests/test_contract_api.py
```
