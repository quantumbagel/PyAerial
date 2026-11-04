# Command Line Interface

PyAerial provides a single CLI executable named `pyaerial` with subcommands for running the ingestion engine, launching the web service, validating configs, and clearing historical data.

## Commands

| Command                 | Description                                                                                                                                                                     |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pyaerial run`          | Starts the tracking engine, which ingests receiver feeds, evaluates integrity scores, triggers alert hooks, and writes both live state to Redis and long-term history to SQLite |
| `pyaerial web`          | Launches the read-only web portal and WebSocket API server, reading state from Redis and SQLite without running tracking logic (documented in [WEBSOCKET.md](WEBSOCKET.md))     |
| `pyaerial validate`     | Inspects configuration files for syntax errors, unknown keys, invalid values, and missing referenced files without starting background services                                 |
| `pyaerial reset [icao]` | Purges historical records from the SQLite archive, either wiping all recorded data or deleting flights for a specific aircraft                                                  |
| `pyaerial --version`    | Displays the installed PyAerial version                                                                                                                                         |

## Options

| Option                | Commands     | Description                                                                                                                                                           |
| --------------------- | ------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `-c`, `--config PATH` | all          | Path to the YAML configuration file (falls back to `$PYAERIAL_CONFIG`, or defaults to `config.yaml`)                                                                  |
| `--aircraft-db PATH`  | `run`, `web` | Path to the cached HexDB and Planespotters SQLite database (falls back to `$PYAERIAL_AIRCRAFT_DB`, or defaults to `aircraft.db` in the current working directory)     |
| `--host ADDR`         | `web`        | Network address the web service should bind to (defaults to `127.0.0.1`; set to `0.0.0.0` inside containers or to bind all interfaces)                                |
| `-p`, `--port PORT`   | `web`        | HTTP and WebSocket port number between 1 and 65535 (defaults to `10090`)                                                                                              |
| `icao`                | `reset`      | Hexadecimal 24-bit ICAO address of a single aircraft to delete (such as `a1b2c3`)                                                                                     |
| `-y`, `--yes`         | `reset`      | Confirms deletion automatically without prompting, which is required when running from non-interactive scripts                                                        |
| `--force`             | `reset`      | Forces the purge to continue when Redis is offline and engine status cannot be verified, leaving Redis untouched while still refusing to run against an active engine |

## Resetting archived data

The `reset` command prevents accidental data loss by refusing to run whenever the engine heartbeat is active. When targeting a specific ICAO address, it checks whether that aircraft is currently airborne and refuses if it is live. Once the engine is stopped, `reset` purges matching live records from Redis as well as from SQLite. If the SQLite archive is corrupted or was created by an incompatible version, running a full reset safely recreates the database schema from scratch.

```bash
# Remove all archived flights, telemetry samples, integrity verdicts, raw frames, and alert logs
pyaerial reset --yes

# Remove historical data for a specific aircraft
pyaerial reset --yes a1b2c3

# Clear the database when Redis is offline (be sure pyaerial run is stopped first)
pyaerial reset --yes --force
```

## Exit codes

Command names and these exit status codes are guaranteed stable throughout the 1.x series, although specific log message phrasing may change over time:

| Code | Meaning                                                                                                                                                                        |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `0`  | Command finished successfully                                                                                                                                                  |
| `1`  | Configuration failed validation (`pyaerial validate` prints `INVALID`), the engine failed during startup, or a `reset` attempt was rejected, canceled, or encountered an error |
| `2`  | Command usage error, such as omitting a subcommand, providing an unknown flag, or passing an invalid argument value                                                            |

## Environment variables

Environment variables can override values specified in your configuration file. If an invalid value is supplied through an environment variable, PyAerial reports the exact variable name in the error output. Explicit command line flags (`-c` and `--aircraft-db`) take precedence over their respective environment variables.

| Variable                       | Overrides                      | Notes                                                                                                     |
| ------------------------------ | ------------------------------ | --------------------------------------------------------------------------------------------------------- |
| `PYAERIAL_CONFIG`              | Configuration file path        | Used as the default path for the `-c` flag                                                                |
| `PYAERIAL_AIRCRAFT_DB`         | Aircraft metadata cache path   | Used as the default path for the `--aircraft-db` flag                                                     |
| `PYAERIAL_HISTORY`             | `database.path`                | Sets the path to the SQLite research archive                                                              |
| `PYAERIAL_REDIS`               | `database.redis_uri`           | Connection string for Redis                                                                               |
| `PYAERIAL_REDIS_PASSWORD_FILE` | `database.redis_password_file` | Path to a file containing the Redis password, in which case `PYAERIAL_REDIS` must not include credentials |
| `PYAERIAL_LOG_LEVEL`           | `logging.level`                | Case-insensitive log level (`debug`, `info`, `warn`, `warning`, `error`)                                  |
| `PYAERIAL_LOG_FILE`            | `logging.file`                 | Destination file for log output                                                                           |
| `PYAERIAL_HZ`                  | `tracking.hz`                  | Tracking loop evaluation frequency in Hertz                                                               |
| `PYAERIAL_WEB_ORIGINS`         | `web.origins`                  | Comma-separated list of allowed browser origins                                                           |
| `PYAERIAL_WEB_ALLOWED_HOSTS`   | `web.allowed_hosts`            | Comma-separated list of accepted HTTP Host headers                                                        |
| `PYAERIAL_BEAST_HOST`          | `web.beast_host`               | Network address for the outgoing Beast TCP server                                                         |
| `PYAERIAL_BEAST_PORT`          | `web.beast_port`               | Outgoing Beast TCP port; setting to empty, `off`, `none`, or `disabled` shuts off the TCP feed            |

In Docker environments, the default container command launches dump1090 internally unless disabled with `PYAERIAL_START_DUMP1090=0`. Detailed Compose settings including `REDIS_PASSWORD` and `DUMP1090_HOST` are described in [`.env.example`](.env.example). Variable substitutions such as `${VAR}` and secret file paths like `${file:...}` inside YAML files are explained in [CONFIGURATION.md](CONFIGURATION.md).
