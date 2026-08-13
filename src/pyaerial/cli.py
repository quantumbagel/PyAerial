"""
PyAerial command-line interface.

Subcommands::

    run        Start the tracking engine
    validate   Check a configuration file without running
    view       Interactive flight viewer for saved & live flights
    live       Live flight terminal display
    web        Start live flight tracker web portal
"""
from __future__ import annotations

import argparse
import logging
import sys

from pyaerial import __version__
from pyaerial.config import ConfigError, load_config
from pyaerial.constants import DEFAULT_AIRCRAFT_DB, DEFAULT_CONFIG_FILE
from pyaerial.engine import run_engine
from pyaerial.logging_setup import setup_logging
from pyaerial.view import run_live_cmd, run_view

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

    view_p = sub.add_parser("view", help="interactive flight viewer (saved & live data)")
    view_p.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE)
    view_p.add_argument("--aircraft-db", default=DEFAULT_AIRCRAFT_DB)
    view_p.add_argument("--mock", action="store_true", help="use mock live data store")
    view_p.set_defaults(func=_cmd_view)

    live_p = sub.add_parser("live", help="live flight display")
    live_p.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE)
    live_p.add_argument("--aircraft-db", default=DEFAULT_AIRCRAFT_DB)
    live_p.add_argument("--mock", action="store_true", help="use mock live data store")
    live_p.add_argument("--interval", type=float, default=1.0, help="display refresh interval in seconds (default: 1.0)")
    live_p.add_argument("-n", "--once", action="store_true", help="print a single frame and exit")
    live_p.set_defaults(func=_cmd_live)

    web_p = sub.add_parser("web", help="start the live flight tracker web application")
    web_p.add_argument("-c", "--config", default=DEFAULT_CONFIG_FILE,
                       help=f"configuration file (default: {DEFAULT_CONFIG_FILE})")
    web_p.add_argument("--aircraft-db", default=DEFAULT_AIRCRAFT_DB,
                       help=f"SQLite aircraft index (default: {DEFAULT_AIRCRAFT_DB})")
    web_p.add_argument("--host", default="0.0.0.0", help="host to bind (default: 0.0.0.0)")
    web_p.add_argument("-p", "--port", type=int, default=10090, help="port to bind (default: 10090)")
    web_p.add_argument("--mock", action="store_true", help="run in mock mode with simulated dummy data (no Redis/MongoDB required)")
    web_p.set_defaults(func=_cmd_web)

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
    print(f"  database: {config.database.uri}")
    print(f"  hz: {config.tracking.hz}")


def _cmd_view(args: argparse.Namespace) -> None:
    setup_logging("warning")
    run_view(args.config, aircraft_db_path=args.aircraft_db, mock=args.mock)


def _cmd_live(args: argparse.Namespace) -> None:
    setup_logging("warning")
    run_live_cmd(args.config, aircraft_db_path=args.aircraft_db, mock=args.mock, interval=args.interval, once=args.once)


def _cmd_web(args: argparse.Namespace) -> None:
    setup_logging("info")
    from pyaerial.webapp import run_webapp
    run_webapp(args.config, aircraft_db_path=args.aircraft_db, host=args.host, port=args.port, mock=args.mock)


if __name__ == "__main__":
    main()

