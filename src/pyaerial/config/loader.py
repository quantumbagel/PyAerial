"""
Loading and validation of the PyAerial configuration file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import ruamel.yaml
from pydantic import ValidationError

from pyaerial.config.schema import Config

log = logging.getLogger("pyaerial.config")


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or is invalid."""


_ENV_OVERRIDES = {
    "PYAERIAL_MONGODB": ("database", "uri"),
    "PYAERIAL_REDIS": ("database", "redis_uri"),
    "PYAERIAL_LOG_LEVEL": ("logging", "level"),
    "PYAERIAL_LOG_FILE": ("logging", "file"),
    "PYAERIAL_HZ": ("tracking", "hz"),
}


def _apply_overrides(data: dict, overrides: dict[str, object] | None) -> dict:
    for env_var, (section, key) in _ENV_OVERRIDES.items():
        if env_var in os.environ:
            data.setdefault(section, {})[key] = os.environ[env_var]
    for dotted, value in (overrides or {}).items():
        section, _, key = dotted.partition(".")
        if not key:
            data[section] = value
        else:
            data.setdefault(section, {})[key] = value
    return data


def load_config(
    path: str | os.PathLike = "config.yaml",
    *,
    overrides: dict[str, object] | None = None,
) -> Config:
    """
    Load, override, and validate the configuration.

    :param path: path to the YAML configuration file
    :param overrides: optional mapping of ``"section.key"`` -> value applied on
        top of the file (and after env-var overrides)
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

    data = _apply_overrides(data, overrides)

    try:
        config = Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(config_path, exc)) from exc

    _validate_cross_references(config, config_path)

    log.debug("Loaded configuration from %s", config_path)
    return config


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
                    elif not _webhook_url_allowed(str(url)):
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


def _webhook_url_allowed(url: str) -> bool:
    lowered = url.strip().lower()
    if lowered.startswith("https://"):
        return True
    if lowered.startswith("http://localhost") or lowered.startswith(
        "http://127.0.0.1"
    ):
        return True
    return False


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    lines = [f"configuration file {path} is invalid:"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)
