# CLI

`pyaerial` operates the tracking pipeline, configuration validator, historical database purger, and real-time web portal.

**Subcommands**

| Subcommand | Process role |
|------------|--------------|
| `pyaerial run` | Tracking engine (ingests frames, writes Redis live state and SQLite history) |
| `pyaerial web` | Read-only API server and web portal (reads Redis and SQLite; does not track) |
| `pyaerial validate` | Syntax, schema, and filesystem cross-reference verification |
| `pyaerial reset` | Retention purger (`--yes`; optional single ICAO; clears Redis only when engine is stopped) |

The web portal serves `GET /health`, `GET /ready`, and `GET /api` schema endpoints alongside WebSocket telemetry feeds. Live flight state broadcasts over `ws://<host>:<port>/ws/live` (with `/ws` supported as an alias), whereas high-volume RF sensor frames stream exclusively over `/ws/raw`. Comprehensive message schemas, RPC actions, and client integration libraries are detailed in [WEBSOCKET.md](WEBSOCKET.md).

**Command execution**

```bash
# Start tracking engine with custom config and aircraft metadata cache
pyaerial run -c /path/to/config.yaml --aircraft-db /path/to/aircraft.db

# Validate configuration syntax and referenced geometries without starting services
pyaerial validate -c config.yaml

# Start web portal bound to all network interfaces
pyaerial web -c config.yaml --host 0.0.0.0 --port 10090

# Purge all retained flights, tracks, and alerts from SQLite
pyaerial reset --yes

# Purge history for a single ICAO target
pyaerial reset --yes abc123

# Stream recorded hex capture through engine (paths resolve relative to replay.yaml)
pyaerial run -c src/pyaerial/examples/replay.yaml
```

**Environment overrides**

Runtime settings accept environment variable overrides; explicit `-c` flags take precedence over `PYAERIAL_CONFIG`.

| Variable | Target configuration key | Behavior |
|----------|--------------------------|----------|
| `PYAERIAL_CONFIG` | Config path | Default path when `-c` is omitted |
| `PYAERIAL_HISTORY` | `database.path` | SQLite history database file path |
| `PYAERIAL_AIRCRAFT_DB` | `--aircraft-db` default | HexDB / Planespotters cache path (`aircraft.db`) |
| `PYAERIAL_REDIS` | `database.redis_uri` | Redis connection URI |
| `PYAERIAL_LOG_LEVEL` | `logging.level` | Logging severity (`debug`, `info`, `warning`, `error`) |
| `PYAERIAL_LOG_FILE` | `logging.file` | Destination log file path |
| `PYAERIAL_HZ` | `tracking.hz` | Engine evaluation frequency in Hz |
| `PYAERIAL_WEB_ORIGINS` | `web.origins` | Allowed browser origins (`*` permits all) |

Configuration strings support `${VAR}` and `${VAR:-default}` environment interpolation, but referenced variables without defaults must be set in the host environment or startup validation fails.

```yaml
on_activate:
  - method: webhook
    options:
      url: "${PYAERIAL_WEBHOOK_URL}"
      format: discord
```

The full YAML schema is in [CONFIGURATION.md](CONFIGURATION.md).
