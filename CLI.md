# CLI

**Commands**

| Command | Role |
|---------|------|
| `pyaerial run` | Engine: ingests receivers, scores integrity, alerts, writes Redis live state and the SQLite archive |
| `pyaerial web` | Read-only portal and API server (reads Redis and SQLite; does not track). Endpoints: [WEBSOCKET.md](WEBSOCKET.md) |
| `pyaerial validate` | Checks syntax, schema and referenced files without starting anything |
| `pyaerial reset [icao]` | Deletes archived history: everything, or one aircraft |
| `pyaerial --version` | Prints the version |

**Options**

| Option | Commands | Meaning |
|--------|----------|---------|
| `-c`, `--config PATH` | all | Config file (default `$PYAERIAL_CONFIG`, else `config.yaml`) |
| `--aircraft-db PATH` | `run`, `web` | HexDB / Planespotters cache (default `$PYAERIAL_AIRCRAFT_DB`, else `aircraft.db` in the working directory) |
| `--host ADDR` | `web` | Bind address (default `127.0.0.1`; `0.0.0.0` for containers or other interfaces) |
| `-p`, `--port PORT` | `web` | Port, 1-65535 (default `10090`) |
| `icao` | `reset` | Delete only this aircraft (6 hex digits, e.g. `a1b2c3`) |
| `-y`, `--yes` | `reset` | Skip the confirmation prompt (required when stdin is not a terminal) |
| `--force` | `reset` | Proceed when Redis is unreachable and the engine state is unknown; Redis is then not cleared. Never overrides a running engine |

`reset` refuses while the engine heartbeat is fresh; with an `icao`, only while that aircraft is live. With the engine stopped it also clears the matching Redis live state. A full reset also recreates an archive it cannot open (newer schema, or not a PyAerial archive).

```bash
pyaerial reset --yes              # delete all flights, telemetry, verdicts, raw frames and alerts
pyaerial reset --yes a1b2c3       # one aircraft
pyaerial reset --yes --force      # Redis down; make sure `pyaerial run` is stopped first
```

**Exit codes**

Commands and these codes are stable within 1.x; log text is not (see Compatibility in the [README](README.md)).

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | Invalid configuration (`validate` prints `INVALID`), engine startup error, or `reset` refused, aborted or failed |
| `2` | Usage error: no command, unknown option, or invalid argument (port, ICAO) |

**Environment variables**

These replace the corresponding config values. An invalid value is reported with the variable's name. `-c` and `--aircraft-db` take precedence over `PYAERIAL_CONFIG` and `PYAERIAL_AIRCRAFT_DB`.

| Variable | Sets | Notes |
|----------|------|-------|
| `PYAERIAL_CONFIG` | Config path | Default for `-c` |
| `PYAERIAL_AIRCRAFT_DB` | Aircraft cache path | Default for `--aircraft-db` |
| `PYAERIAL_HISTORY` | `database.path` | |
| `PYAERIAL_REDIS` | `database.redis_uri` | |
| `PYAERIAL_REDIS_PASSWORD_FILE` | `database.redis_password_file` | The URI must not contain a password then |
| `PYAERIAL_LOG_LEVEL` | `logging.level` | |
| `PYAERIAL_LOG_FILE` | `logging.file` | |
| `PYAERIAL_HZ` | `tracking.hz` | |
| `PYAERIAL_WEB_ORIGINS` | `web.origins` | Comma-separated |
| `PYAERIAL_WEB_ALLOWED_HOSTS` | `web.allowed_hosts` | Comma-separated |
| `PYAERIAL_BEAST_HOST` | `web.beast_host` | |
| `PYAERIAL_BEAST_PORT` | `web.beast_port` | `off`, `none`, `disable`, `disabled` or empty disables Beast TCP |

The Docker image's default command also starts dump1090 in the container unless `PYAERIAL_START_DUMP1090=0`. Compose settings such as `REDIS_PASSWORD` and `DUMP1090_HOST` are explained in [`.env.example`](.env.example). For `${VAR}` and `${file:...}` inside the config file, see [CONFIGURATION.md](CONFIGURATION.md).
