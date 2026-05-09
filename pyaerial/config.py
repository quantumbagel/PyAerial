"""
Load and resolve PyAerial configuration (YAML).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import ruamel.yaml

from pyaerial.constants import CONFIG_FILE, ENV_CONFIG_PATH


def resolve_config_path(explicit: str | None = None) -> Path:
    """
    Resolve path to config file: CLI argument > PYAERIAL_CONFIG > ./config.yaml
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get(ENV_CONFIG_PATH)
    if env:
        return Path(env).expanduser().resolve()
    return (Path.cwd() / CONFIG_FILE).resolve()


def load_configuration(path: Path) -> dict:
    """
    Load YAML configuration from path. Exits the process on missing file or parse error.
    """
    yaml = ruamel.yaml.YAML(typ="safe")
    try:
        with open(path) as config:
            try:
                data = yaml.load(config)
            except ruamel.yaml.scanner.ScannerError:
                print(
                    f"[critical] PyAerial failed to load configuration (YAML parse error). "
                    f"Check {path}."
                )
                sys.exit(1)
    except FileNotFoundError:
        print(f"[critical] Configuration file not found: {path}")
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"[critical] Configuration root must be a mapping, got {type(data).__name__}")
        sys.exit(1)
    return data
