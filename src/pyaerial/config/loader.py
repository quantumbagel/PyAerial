"""Configuration loader with environment interpolation and schema validation."""

from __future__ import annotations

import difflib
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import ruamel.yaml
from pydantic import ValidationError

from pyaerial.config.geofence_file import GeofenceFileError, load_geofence_coordinates
from pyaerial.config.schema import AlertActionConfig, Config, ReceiverConfig
from pyaerial.urls import redact_url, webhook_url_allowed

log = logging.getLogger("pyaerial.config")

_ENV_INTERPOLATION = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}"
)
_FILE_INTERPOLATION = re.compile(r"\$\{file:([^}]+)\}")
_DUMP1090_FORMATS = frozenset({"avr", "raw", "beast", "binary"})


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or is invalid."""


_ENV_OVERRIDES = {
    "PYAERIAL_HISTORY": ("database", "path"),
    "PYAERIAL_REDIS": ("database", "redis_uri"),
    "PYAERIAL_REDIS_PASSWORD_FILE": ("database", "redis_password_file"),
    "PYAERIAL_LOG_LEVEL": ("logging", "level"),
    "PYAERIAL_LOG_FILE": ("logging", "file"),
    "PYAERIAL_HZ": ("tracking", "hz"),
    "PYAERIAL_WEB_ORIGINS": ("web", "origins"),
    "PYAERIAL_WEB_ALLOWED_HOSTS": ("web", "allowed_hosts"),
    "PYAERIAL_BEAST_PORT": ("web", "beast_port"),
    "PYAERIAL_BEAST_HOST": ("web", "beast_host"),
}


def _apply_env_overrides(
    data: dict, applied: dict[tuple[str, str], str] | None = None
) -> dict:
    """Apply PYAERIAL_* overrides; record ``(section, key) -> variable`` in ``applied``."""
    for env_var, (section, key) in _ENV_OVERRIDES.items():
        if env_var not in os.environ:
            continue
        value: object = os.environ[env_var]
        if env_var in ("PYAERIAL_WEB_ORIGINS", "PYAERIAL_WEB_ALLOWED_HOSTS"):
            value = [part.strip() for part in str(value).split(",") if part.strip()]
        elif env_var == "PYAERIAL_BEAST_PORT":
            raw = str(value).strip().lower()
            if raw in {"", "none", "off", "disable", "disabled"}:
                value = None
            else:
                try:
                    value = int(raw)
                except ValueError:
                    raise ConfigError(
                        f"{env_var}={os.environ[env_var]!r} must be a port "
                        "number or 'off'"
                    ) from None
        target = data.get(section)
        if target is None:
            target = data[section] = {}
        if isinstance(target, dict):
            target[key] = value
            if applied is not None:
                applied[(section, key)] = env_var
        # A non-mapping section is left for schema validation to report.
    return data


def load_config(
    path: str | os.PathLike = "config.yaml",
) -> Config:
    """
    Load, override, and validate the configuration.

    :param path: path to the YAML configuration file
    :raises ConfigError: if the file is missing, malformed, or invalid
    """
    config_path = Path(path)
    yaml = ruamel.yaml.YAML(typ="safe")
    try:
        with config_path.open(encoding="utf-8") as handle:
            data = yaml.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file {config_path} does not exist") from exc
    except ruamel.yaml.YAMLError as exc:
        raise ConfigError(f"could not parse {config_path}: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"could not read {config_path}: {exc}") from exc

    if data is None:
        raise ConfigError(f"configuration file {config_path} is empty")
    if not isinstance(data, dict):
        raise ConfigError(
            f"configuration file {config_path} must contain a mapping at the top level"
        )

    # A section whose keys are all commented out parses as null; treat it as
    # absent so defaults apply (and required sections report "missing").
    data = {key: value for key, value in data.items() if value is not None}
    data = _expand_env_vars(data, config_path)
    overridden: dict[tuple[str, str], str] = {}
    data = _apply_env_overrides(data, overridden)
    data = _resolve_zone_files(data, config_path)

    try:
        config = Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(
            _format_validation_error(config_path, exc, overridden)
        ) from exc

    _resolve_history_path(config, config_path)
    _apply_redis_password_file(config, config_path)
    _validate_cross_references(config, config_path)
    _validate_polygons(config, config_path)

    log.debug("Loaded configuration from %s", config_path)
    return config


def _validate_polygons(config: Config, config_path: Path) -> None:
    """Build the zone polygons as the engine will, so a rejection is a config error."""
    from pyaerial.calc import geo

    try:
        geo.build_polygons(config.zones)
    except ValueError as exc:
        raise ConfigError(
            f"configuration file {config_path} is invalid:\n  - zones: {exc}"
        ) from exc


_PASSWORD_FILE_SETTING = "database.redis_password_file (PYAERIAL_REDIS_PASSWORD_FILE)"


def _password_file_error(config_path: Path, reason: str) -> ConfigError:
    # Only paths and fixed text: never the file contents or the URI.
    return ConfigError(
        f"configuration file {config_path} is invalid:\n"
        f"  - {_PASSWORD_FILE_SETTING}: {reason}"
    )


def _apply_redis_password_file(config: Config, config_path: Path) -> None:
    """Put the password from ``redis_password_file`` into ``redis_uri``.

    Keeps the secret out of PYAERIAL_REDIS (and so out of ``docker inspect``);
    every Redis client reads the combined URI. Logs use ``redact_uri``.
    """
    raw_path = config.database.redis_password_file
    if not raw_path:
        return
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (config_path.parent / path).resolve()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise _password_file_error(config_path, f"cannot read {path}: {reason}") from None
    except UnicodeDecodeError:
        raise _password_file_error(config_path, f"{path} is not UTF-8 text") from None
    password = text.rstrip("\r\n")
    if not password:
        raise _password_file_error(config_path, f"{path} is empty")
    if "\n" in password or "\r" in password:
        raise _password_file_error(
            config_path, f"{path} must hold the password on one line"
        )
    try:
        parts = urlsplit(config.database.redis_uri)
        inline = parts.password
    except ValueError:
        raise _password_file_error(config_path, "redis_uri is not a valid URI") from None
    if inline:
        raise _password_file_error(
            config_path,
            "database.redis_uri already contains a password; "
            "set it in the URI or in the file, not both",
        )
    userinfo, _, hostport = parts.netloc.rpartition("@")
    username = userinfo.partition(":")[0]
    netloc = f"{username}:{quote(password, safe='')}@{hostport}"
    config.database.redis_uri = urlunsplit(parts._replace(netloc=netloc))


def _resolve_history_path(config: Config, config_path: Path) -> None:
    """Resolve a relative history SQLite path against the config file directory."""
    history_path = Path(config.database.path).expanduser()
    if not history_path.is_absolute():
        history_path = (config_path.parent / history_path).resolve()
    else:
        history_path = history_path.resolve()
    config.database.path = str(history_path)


def _resolve_zone_files(data: dict, config_path: Path) -> dict:
    """Expand ``zones.*.file`` into ``coordinates`` before schema validation."""
    zones = data.get("zones")
    if not isinstance(zones, dict):
        return data
    for name, zone in zones.items():
        if not isinstance(zone, dict):
            continue
        file_ref = zone.get("file")
        if not file_ref:
            continue
        if zone.get("coordinates"):
            raise ConfigError(
                f"configuration file {config_path} is invalid:\n"
                f"  - zones.{name}: provide coordinates or file, not both"
            )
        path = Path(str(file_ref))
        if not path.is_absolute():
            path = (config_path.parent / path).resolve()
        else:
            path = path.resolve()
        if not path.is_file():
            raise ConfigError(
                f"configuration file {config_path} is invalid:\n"
                f"  - zones.{name}.file: not found: {path}"
            )
        try:
            zone["coordinates"] = load_geofence_coordinates(path)
        except GeofenceFileError as exc:
            raise ConfigError(
                f"configuration file {config_path} is invalid:\n"
                f"  - zones.{name}.file: {exc}"
            ) from exc
    return data


def _validate_cross_references(config: Config, path: Path) -> None:
    """Check receiver types, alerter methods, and webhook options."""
    from pyaerial.alerters import available_alerters
    from pyaerial.receivers import available_receivers, unavailable_receivers

    known_receivers = set(available_receivers())
    missing_extras = unavailable_receivers()
    unknown_receivers = []
    extra_problems = []
    for name, cfg in config.receivers.items():
        if cfg.type in known_receivers:
            continue
        if cfg.type in missing_extras:
            extra_problems.append(
                f"receivers.{name}: type {cfg.type!r} is not installed "
                f"({missing_extras[cfg.type]})"
            )
            continue
        unknown_receivers.append(f"{name} ({cfg.type})")
    if extra_problems or unknown_receivers:
        lines = [f"configuration file {path} is invalid:"]
        lines.extend(f"  - {item}" for item in extra_problems)
        if unknown_receivers:
            lines.append(
                "  - receivers: unknown type(s): "
                f"{', '.join(unknown_receivers)}; "
                f"available: {', '.join(sorted(known_receivers))}"
            )
        raise ConfigError("\n".join(lines))

    option_problems = [
        f"receivers.{name}.options: {msg}"
        for name, cfg in config.receivers.items()
        for msg in _unknown_options(
            cfg.options, _known_options(_receiver_class(cfg.type), _RECEIVER_OPTIONS, cfg.type)
        )
    ]
    if option_problems:
        raise ConfigError(
            f"configuration file {path} is invalid:\n"
            + "\n".join(f"  - {item}" for item in option_problems)
        )

    loop_problems = _beast_feedback_problems(config)
    if loop_problems:
        raise ConfigError(
            f"configuration file {path} is invalid:\n"
            + "\n".join(f"  - {item}" for item in loop_problems)
        )

    dump1090_problems: list[str] = []
    for name, cfg in config.receivers.items():
        if cfg.type != "dump1090":
            continue
        # options.format reaches the receiver too; it falls back to AVR (with
        # only a log warning) for names it does not know.
        raw_fmt = cfg.receiver_arguments().get("format")
        fmt = str(raw_fmt).lower() if raw_fmt is not None else None
        port = cfg.port
        if fmt is not None and fmt not in _DUMP1090_FORMATS:
            dump1090_problems.append(
                f"receivers.{name}: format {raw_fmt!r} is not one of "
                "avr, raw, beast, binary"
            )
            continue
        if port == 30002 and fmt in {"beast", "binary"}:
            dump1090_problems.append(
                f"receivers.{name}: format {fmt} is Beast binary; "
                "port 30002 is AVR text. Use port 30005, or format: avr."
            )
        if port == 30005 and fmt in {"avr", "raw"}:
            dump1090_problems.append(
                f"receivers.{name}: format {fmt} is AVR text; "
                "port 30005 is Beast binary. Use port 30002, or format: beast."
            )
    if dump1090_problems:
        raise ConfigError(
            f"configuration file {path} is invalid:\n"
            + "\n".join(f"  - {item}" for item in dump1090_problems)
        )

    replay_problems: list[str] = []
    for name, cfg in config.receivers.items():
        if cfg.type != "replay":
            continue
        raw_path = cfg.options.get("path")
        if not raw_path:
            replay_problems.append(f"receivers.{name}: replay requires options.path")
            continue
        replay_path = Path(str(raw_path)).expanduser()
        if not replay_path.is_absolute():
            replay_path = (path.parent / replay_path).resolve()
        else:
            replay_path = replay_path.resolve()
        cfg.options["path"] = str(replay_path)
        if not replay_path.is_file():
            replay_problems.append(
                f"receivers.{name}.options.path: not found: {replay_path}"
            )
    if replay_problems:
        raise ConfigError(
            f"configuration file {path} is invalid:\n"
            + "\n".join(f"  - {item}" for item in replay_problems)
        )

    # The engine builds receivers at startup and only logs (then skips) one
    # whose options it rejects; build each once here so that fails now.
    receiver_problems = [
        f"receivers.{name}: {msg}"
        for name, cfg in config.receivers.items()
        for msg in _receiver_problems(name, cfg)
    ]
    if receiver_problems:
        raise ConfigError(
            f"configuration file {path} is invalid:\n"
            + "\n".join(f"  - {item}" for item in receiver_problems)
        )

    known_alerters = set(available_alerters())
    problems: list[str] = []
    for zone_name, zone in config.zones.items():
        for rule in zone.rules:
            actions = list(rule.on_activate) + list(rule.on_deactivate)
            if rule.while_active is not None:
                actions.extend(rule.while_active.actions)
            for action in actions:
                loc = f"zones.{zone_name}.rules.{rule.name}"
                if action.method not in known_alerters:
                    problems.append(
                        f"{loc}: unknown alerter {action.method!r}; "
                        f"available: {', '.join(sorted(known_alerters))}"
                    )
                    continue
                found = len(problems)
                known = _known_options(
                    _alerter_class(action.method), _ALERTER_OPTIONS, action.method
                )
                problems.extend(
                    f"{loc}: {action.method} options: {msg}"
                    for msg in _unknown_options(action.options, known)
                )
                if len(problems) > found:
                    continue
                if action.method == "webhook":
                    url = action.options.get("url")
                    if not url:
                        problems.append(f"{loc}: webhook action requires options.url")
                    elif not webhook_url_allowed(str(url)):
                        problems.append(
                            f"{loc}: webhook url must be https "
                            f"(http allowed only for localhost): {redact_url(str(url))}"
                        )
                    method = action.options.get("method")
                    if method is not None and str(method).strip().upper() not in {
                        "POST",
                        "PUT",
                    }:
                        problems.append(f"{loc}: webhook method must be POST or PUT")
                if len(problems) == found:
                    problems.extend(
                        f"{loc}: {msg}" for msg in _alerter_problems(action)
                    )
    if problems:
        lines = [f"configuration file {path} is invalid:"]
        lines.extend(f"  - {item}" for item in problems)
        raise ConfigError("\n".join(lines))


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})


def _beast_feedback_problems(config: Config) -> list[str]:
    """A dump1090 receiver reading this machine's ``web.beast_port`` (the
    portal's re-encoded output) would feed the engine its own frames."""
    port = config.web.beast_port
    if not port:
        return []
    problems = []
    for name, cfg in config.receivers.items():
        if cfg.type != "dump1090":
            continue
        args = cfg.receiver_arguments()
        host = str(args.get("tcp_connection_ip") or "localhost").strip("[]").lower()
        receiver_port = args.get("tcp_connection_port")
        if receiver_port is None:
            receiver_port = 30002 if _dump1090_wire_format(args) == "avr" else 30005
        if host in _LOOPBACK_HOSTS and str(receiver_port) == str(port):
            problems.append(
                f"receivers.{name} reads {host or 'localhost'}:{receiver_port}, which is "
                f"web.beast_port: the engine would ingest its own re-served output. "
                "Use another web.beast_port (default 30105) or PYAERIAL_BEAST_PORT=off"
            )
    return problems


def _dump1090_wire_format(args: dict[str, object]) -> str:
    fmt = args.get("format")
    if fmt is not None:
        return "beast" if str(fmt).lower() in {"beast", "binary"} else "avr"
    port = args.get("tcp_connection_port")
    return "avr" if port is not None and str(port) == "30002" else "beast"


# Option keys each built-in plugin reads (configure(), plus the integrity
# monitor for AoA). A plugin class may declare ``known_options`` instead.
_RECEIVER_OPTIONS: dict[str, frozenset[str]] = {
    "dump1090": frozenset({"format", "tcp_connection_ip", "tcp_connection_port"}),
    "aoa": frozenset(
        {
            "host",
            "port",
            "tcp_connection_ip",
            "tcp_connection_port",
            "bearing_offset",
            "array_axis",
        }
    ),
    "replay": frozenset({"path", "speed", "loop", "interval"}),
}
_ALERTER_OPTIONS: dict[str, frozenset[str]] = {
    "webhook": frozenset({"url", "format", "method", "headers"}),
    "print": frozenset(),
}


def _receiver_class(kind: str) -> type | None:
    from pyaerial.receivers import _REGISTRY

    return _REGISTRY.get(kind)


def _alerter_class(method: str) -> type | None:
    from pyaerial.alerters import _REGISTRY

    return _REGISTRY.get(method)


def _known_options(
    cls: type | None, table: dict[str, frozenset[str]], name: str
) -> frozenset[str] | None:
    declared = getattr(cls, "known_options", None)
    if declared is not None:
        return frozenset(declared)
    # None: a plugin we know nothing about; its options are not checked.
    return table.get(name)


def _unknown_options(options: dict[str, object], known: frozenset[str] | None) -> list[str]:
    """One message per option key the plugin does not read (typos)."""
    if known is None:
        return []
    messages = []
    for key in sorted(str(key) for key in options if str(key) not in known):
        close = difflib.get_close_matches(key, sorted(known), n=1)
        hint = f"did you mean {close[0]!r}? " if close else ""
        valid = ", ".join(sorted(known)) or "none"
        messages.append(f"unknown option {key!r}; {hint}valid: {valid}")
    return messages


def _receiver_problems(name: str, cfg: ReceiverConfig) -> list[str]:
    """Construct the receiver (``configure`` only; no I/O, nothing started)."""
    from pyaerial.receivers import create_receiver

    try:
        create_receiver(cfg.type, name, lambda *_args: None, cfg.receiver_arguments())
    except (ValueError, TypeError, KeyError) as exc:
        return [str(exc).strip("'\"")]
    return []


def _alerter_problems(action: AlertActionConfig) -> list[str]:
    """Build the alerter once so bad options fail now, not at the first alert.

    The alert engine builds alerters lazily and drops the alert (with only a
    log line) when that fails. Construction does no I/O.
    """
    from pyaerial.alerters import create_alerter

    try:
        alerter = create_alerter(action.method, dict(action.options))
    except (ValueError, TypeError, KeyError) as exc:
        return [str(exc)]
    alerter.close()
    return []


def _expand_env_vars(value: object, path: Path) -> object:
    """Replace ``${VAR}`` and ``${VAR:-default}`` in string values.

    Unset variables without a default raise :class:`ConfigError`.
    """
    if isinstance(value, str):
        return _expand_env_string(value, path)
    if isinstance(value, list):
        return [_expand_env_vars(item, path) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env_vars(item, path) for key, item in value.items()}
    return value


def _read_file_reference(reference: str, path: Path) -> str:
    """Contents of a ``${file:...}`` reference (a Docker secret, say).

    Errors name the file, never its contents.
    """
    target = Path(reference.strip()).expanduser()
    if not target.is_absolute():
        target = (path.parent / target).resolve()

    def error(reason: str) -> ConfigError:
        return ConfigError(
            f"configuration file {path} is invalid:\n"
            f"  - ${{file:{reference}}}: {reason}"
        )

    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise error(f"cannot read {target}: {exc.strerror or type(exc).__name__}") from None
    except UnicodeDecodeError:
        raise error(f"{target} is not UTF-8 text") from None
    value = text.rstrip("\r\n")
    if not value:
        raise error(f"{target} is empty")
    if "\n" in value or "\r" in value:
        raise error(f"{target} must hold a single line")
    return value


def _expand_env_string(value: str, path: Path) -> str:
    # ${file:...} first: its contents are used verbatim, never re-expanded.
    if "${file:" in value:
        parts = _FILE_INTERPOLATION.split(value)
        # split() alternates literal text and captured references.
        return "".join(
            _read_file_reference(part, path) if index % 2 else _expand_env_string(part, path)
            for index, part in enumerate(parts)
        )
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        missing.append(name)
        return match.group(0)

    expanded = _ENV_INTERPOLATION.sub(replace, value)
    if missing:
        names = ", ".join(sorted(set(missing)))
        raise ConfigError(
            f"configuration file {path} is invalid:\n"
            f"  - unset environment variable(s): {names}"
        )
    return expanded


def _format_validation_error(
    path: Path,
    exc: ValidationError,
    overridden: dict[tuple[str, str], str] | None = None,
) -> str:
    lines = [f"configuration file {path} is invalid:"]
    for error in exc.errors():
        loc = tuple(str(part) for part in error["loc"])
        location = ".".join(loc) or "<root>"
        env_var = (overridden or {}).get(loc[:2])
        if env_var is not None:
            # The bad value came from the environment, not the file.
            location = f"{env_var}={os.environ.get(env_var, '')!r} (sets {location})"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)
