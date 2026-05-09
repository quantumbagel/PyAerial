"""
CLI entry: load config, validate, start receivers, run main loop.
"""
from __future__ import annotations

import argparse
import logging
import sys

from pyaerial import constants
from pyaerial.config import load_configuration, resolve_config_path
from pyaerial import mainloop, receivers
from pyaerial.validator import validate_config


def main() -> None:
    parser = argparse.ArgumentParser(prog="pyaerial", description="ADS-B / Mode S geofence tracker")
    parser.add_argument(
        "-c",
        "--config",
        default=None,
        help="Path to YAML config (default: $PYAERIAL_CONFIG or ./config.yaml)",
    )
    args = parser.parse_args()

    path = resolve_config_path(args.config)
    cfg = load_configuration(path)

    level_name = cfg[constants.CONFIG_GENERAL][constants.CONFIG_GENERAL_LOGGING_LEVEL]
    logging.basicConfig(level=constants.LOGGING_LEVELS[level_name])
    constants.CONFIGURATION = cfg

    issues = validate_config(cfg)
    if any(sev == "error" for sev, _ in issues):
        sys.exit(1)

    receivers.load_interfaces(cfg)
    logging.getLogger("pyaerial").info("Configuration loaded from %s", path)
    mainloop.run_forever(cfg)


if __name__ == "__main__":
    main()
