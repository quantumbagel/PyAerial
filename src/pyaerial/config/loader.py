"""
Loading and validation of the PyAerial configuration file.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import ruamel.yaml
from pydantic import ValidationError

from pyaerial.config.geofence_file import GeofenceFileError, load_geofence_coordinates
from pyaerial.config.schema import Config

log = logging.getLogger("pyaerial.config")

_ENV_INTERPOLATION = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}"
)
_LOCAL_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or is invalid."""


_ENV_OVERRIDES = {
    "PYAERIAL_MONGODB": ("database", "uri"),
    "PYAERIAL_REDIS": ("database", "redis_uri"),
    "PYAERIAL_LOG_LEVEL": ("logging", "level"),
    "PYAERIAL_LOG_FILE": ("logging", "file"),
    "PYAERIAL_HZ": ("tracking", "hz"),
    "PYAERIAL_WEB_TOKEN": ("web", "token"),
}


def _apply_env_overrides(data: dict) -> dict:
    for env_var, (section, key) in _ENV_OVERRIDES.items():
        if env_var in os.environ:
            data.setdefault(section, {})[key] = os.environ[env_var]
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
        with config_path.open() as handle:
            data = yaml.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file {config_path} does not exist") from exc
    except ruamel.yaml.YAMLError as exc:
        raise ConfigError(f"could not parse {config_path}: {exc}") from exc

    if data is None:
        raise ConfigError(f"configuration file {config_path} is empty")
    if not isinstance(data, dict):
        raise ConfigError(
            f"configuration file {config_path} must contain a mapping at the top level"
        )

    data = _expand_env_vars(data, config_path)
    data = _apply_env_overrides(data)
    data = _resolve_zone_files(data, config_path)

    try:
        config = Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(config_path, exc)) from exc

    _validate_cross_references(config, config_path)

    log.debug("Loaded configuration from %s", config_path)
    return config


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
    from pyaerial.receivers import available_receivers

    known_receivers = set(available_receivers())
    unknown_receivers = [
        f"{name} ({cfg.type})"
        for name, cfg in config.receivers.items()
        if cfg.type not in known_receivers
    ]
    if unknown_receivers:
        raise ConfigError(
            f"configuration file {path} is invalid:\n"
            f"  - receivers: unknown type(s): {', '.join(unknown_receivers)}; "
            f"available: {', '.join(sorted(known_receivers))}"
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
                if action.method == "webhook":
                    url = action.options.get("url")
                    if not url:
                        problems.append(f"{loc}: webhook action requires options.url")
                    elif not webhook_url_allowed(str(url)):
                        problems.append(
                            f"{loc}: webhook url must be https "
                            f"(http allowed only for localhost): {url}"
                        )
                if action.method == "kafka" and "server" not in action.options:
                    problems.append(f"{loc}: kafka action requires options.server")
    if problems:
        lines = [f"configuration file {path} is invalid:"]
        lines.extend(f"  - {item}" for item in problems)
        raise ConfigError("\n".join(lines))


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


def _expand_env_string(value: str, path: Path) -> str:
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


def webhook_url_allowed(url: str) -> bool:
    """HTTPS anywhere, HTTP only for localhost / 127.0.0.1 / ::1."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if parsed.scheme == "https":
        return True
    if parsed.scheme == "http":
        return host in _LOCAL_HTTP_HOSTS
    return False


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    lines = [f"configuration file {path} is invalid:"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)
