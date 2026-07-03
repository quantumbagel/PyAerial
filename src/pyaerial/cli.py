"""
PyAerial command-line interface.

Subcommands::

    run        Start the tracking engine
    validate   Check a configuration file without running
    statview   Interactive browser for saved MongoDB flights
    build-db   Build the SQLite aircraft metadata index from database.csv
"""
from __future__ import annotations

import argparse
import logging
import sys

from pyaerial import __version__
from pyaerial.calc.aircraft_db import build_from_csv
from pyaerial.config import ConfigError, load_config
from pyaerial.constants import DEFAULT_AIRCRAFT_CSV, DEFAULT_AIRCRAFT_DB, DEFAULT_CONFIG_FILE
from pyaerial.engine import run_engine
from pyaerial.logging_setup import setup_logging
from pyaerial.statview import run_statview

log = logging.getLogger("pyaerial.cli")


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        sys.exit(0)
    args.func(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyaerial",
        description="PyAerial ADS-B / Mode S tracking and geofence alerting",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="start the tracking engine")
    run_p.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE,
                       help=f"configuration file (default: {DEFAULT_CONFIG_FILE})")
    run_p.add_argument("--aircraft-db", default=DEFAULT_AIRCRAFT_DB,
                       help=f"SQLite aircraft index (default: {DEFAULT_AIRCRAFT_DB})")
    run_p.set_defaults(func=_cmd_run)

    val_p = sub.add_parser("validate", help="validate a configuration file")
    val_p.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE)
    val_p.set_defaults(func=_cmd_validate)

    sv_p = sub.add_parser("statview", help="browse saved flights in MongoDB")
    sv_p.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE)
    sv_p.add_argument("--aircraft-db", default=DEFAULT_AIRCRAFT_DB)
    sv_p.set_defaults(func=_cmd_statview)

    db_p = sub.add_parser("build-db", help="build SQLite aircraft index from OpenSky CSV")
    db_p.add_argument("--csv", default=DEFAULT_AIRCRAFT_CSV,
                      help=f"OpenSky CSV export (default: {DEFAULT_AIRCRAFT_CSV})")
    db_p.add_argument("-o", "--output", default=DEFAULT_AIRCRAFT_DB,
                      help=f"output SQLite database (default: {DEFAULT_AIRCRAFT_DB})")
    db_p.set_defaults(func=_cmd_build_db)

    return parser


def _cmd_run(args: argparse.Namespace) -> None:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        sys.exit(1)
    run_engine(config, aircraft_db_path=args.aircraft_db)


def _cmd_validate(args: argparse.Namespace) -> None:
    setup_logging("warning")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"INVALID:\n{exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Configuration {args.config!r} is valid.")
    print(f"  receivers: {', '.join(config.receivers)}")
    print(f"  zones: {', '.join(config.zones) or '(none)'}")
    print(f"  categories: {', '.join(config.categories) or '(none)'}")
    print(f"  saver: {config.general.saver}")
    print(f"  hz: {config.general.hz}")


def _cmd_statview(args: argparse.Namespace) -> None:
    setup_logging("warning")
    run_statview(args.config, aircraft_db_path=args.aircraft_db)


def _cmd_build_db(args: argparse.Namespace) -> None:
    setup_logging("info")
    try:
        count = build_from_csv(args.csv, args.output)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Indexed {count} aircraft into {args.output}")


if __name__ == "__main__":
    main()
