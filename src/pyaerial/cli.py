"""
PyAerial command-line interface.

    run        Start the tracking engine (writes Redis and SQLite)
    validate   Check a configuration file without running
    reset      Wipe retained history (live Redis only if the engine is stopped)
    web        Start the web portal (reads Redis and SQLite; does not track)
"""

from __future__ import annotations

import argparse
import sys

from pyaerial import __version__
from pyaerial.config import ConfigError, load_config
from pyaerial.constants import DEFAULT_AIRCRAFT_DB, DEFAULT_CONFIG_FILE
from pyaerial.engine import run_engine
from pyaerial.logging_setup import setup_logging
from pyaerial.view import run_reset


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
        description="ADS-B / Mode S tracking with zone rules, alerts, Redis live state, and SQLite history",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="start the tracking engine")
    run_p.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_FILE,
        help=f"configuration file (default: {DEFAULT_CONFIG_FILE})",
    )
    run_p.add_argument(
        "--aircraft-db",
        default=DEFAULT_AIRCRAFT_DB,
        help=f"SQLite aircraft index (default: {DEFAULT_AIRCRAFT_DB})",
    )
    run_p.set_defaults(func=_cmd_run)

    val_p = sub.add_parser("validate", help="validate a configuration file")
    val_p.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE)
    val_p.set_defaults(func=_cmd_validate)

    reset_p = sub.add_parser(
        "reset",
        help="wipe retained history (live Redis only if the engine is stopped)",
    )
    reset_p.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE)
    reset_p.add_argument(
        "icao",
        nargs="?",
        default=None,
        help="delete one ICAO from history instead of wiping everything",
    )
    reset_p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="do not prompt for confirmation",
    )
    reset_p.set_defaults(func=_cmd_reset)

    web_p = sub.add_parser("web", help="start the live flight tracker web application")
    web_p.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_FILE,
        help=f"configuration file (default: {DEFAULT_CONFIG_FILE})",
    )
    web_p.add_argument(
        "--aircraft-db",
        default=DEFAULT_AIRCRAFT_DB,
        help=f"SQLite aircraft index (default: {DEFAULT_AIRCRAFT_DB})",
    )
    web_p.add_argument(
        "--host",
        default="127.0.0.1",
        help="host to bind (default: 127.0.0.1; use 0.0.0.0 for LAN)",
    )
    web_p.add_argument(
        "-p", "--port", type=int, default=10090, help="port to bind (default: 10090)"
    )
    web_p.set_defaults(func=_cmd_web)

    return parser


def _cmd_run(args: argparse.Namespace) -> None:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        sys.exit(1)
    try:
        run_engine(config, aircraft_db_path=args.aircraft_db)
    except RuntimeError as exc:
        print(f"Engine error:\n{exc}", file=sys.stderr)
        sys.exit(1)


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
    print(f"  history: {config.database.path}")
    print(f"  hz: {config.tracking.hz}")


def _cmd_reset(args: argparse.Namespace) -> None:
    setup_logging("warning")
    try:
        run_reset(args.config, icao=args.icao, yes=args.yes)
    except ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_web(args: argparse.Namespace) -> None:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        sys.exit(1)
    setup_logging(config.logging.level, log_file=config.logging.file)
    from pyaerial.webapp import run_webapp

    run_webapp(
        args.config,
        aircraft_db_path=args.aircraft_db,
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
