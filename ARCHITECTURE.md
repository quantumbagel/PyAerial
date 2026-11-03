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
pyaerial.constants | pyaerial.units | pyaerial.jsonutil | pyaerial.urls
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
- `RedisLiveStore.retry_pending_pops`: retries failed Redis deletes.

Archive schema upgrades (`store/history.py`) run in one transaction.

**Adding a receiver or alerter**

- Receiver: subclass `pyaerial.receivers.Receiver`, implement `run()` (call `self.emit(...)` per frame, poll `should_stop()`, return a reason when exiting), decorate it with `@register_receiver("name")`, and import it in `register_builtins()` in `receivers/__init__.py`. Top-level `host` / `port` reach only `dump1090` and `aoa` (`ReceiverConfig.receiver_arguments`); other receivers read `options`.
- Alerter: subclass `pyaerial.alerters.Alerter`, implement `alert(meta, payload)`, decorate it with `@register_alerter("name")`, and import it in `register_builtins()` in `alerters/__init__.py`.
- Declare `known_options` on the class (or extend the tables in `config/loader.py`) so `validate` rejects typos.
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
