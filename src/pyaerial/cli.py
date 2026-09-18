"""
Command-line interface for PyAerial tracking, validation, maintenance, and portal services.

    run        Start tracking engine and populate Redis/SQLite
    validate   Validate configuration syntax, schema, and referenced paths
    reset      Purge retained flight history (and live Redis if engine is stopped)
    web        Serve real-time WebSocket feeds and frontend portal
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
        description="ADS-B / Mode S tracking engine with polygon rules, Redis live state, and SQLite history",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="start tracking engine")
    run_p.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_FILE,
        help=f"configuration file path (default: {DEFAULT_CONFIG_FILE})",
    )
    run_p.add_argument(
        "--aircraft-db",
        default=DEFAULT_AIRCRAFT_DB,
        help=f"SQLite aircraft metadata cache (default: {DEFAULT_AIRCRAFT_DB})",
    )
    run_p.set_defaults(func=_cmd_run)

    val_p = sub.add_parser("validate", help="validate configuration file syntax and schema")
    val_p.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_FILE,
        help=f"configuration file path (default: {DEFAULT_CONFIG_FILE})",
    )
    val_p.set_defaults(func=_cmd_validate)

    reset_p = sub.add_parser(
        "reset",
        help="purge retained flight history (and live Redis if engine is stopped)",
    )
    reset_p.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_FILE,
        help=f"configuration file path (default: {DEFAULT_CONFIG_FILE})",
    )
    reset_p.add_argument(
        "icao",
        nargs="?",
        default=None,
        help="target single ICAO address for deletion instead of full database",
    )
    reset_p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="bypass interactive confirmation prompt",
    )
    reset_p.set_defaults(func=_cmd_reset)

    web_p = sub.add_parser("web", help="start web portal and WebSocket API server")
    web_p.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_FILE,
        help=f"configuration file path (default: {DEFAULT_CONFIG_FILE})",
    )
    web_p.add_argument(
        "--aircraft-db",
        default=DEFAULT_AIRCRAFT_DB,
        help=f"SQLite aircraft metadata cache (default: {DEFAULT_AIRCRAFT_DB})",
    )
    web_p.add_argument(
        "--host",
        default="127.0.0.1",
        help="network interface to bind (default: 127.0.0.1; use 0.0.0.0 for LAN/WAN)",
    )
    web_p.add_argument(
        "-p", "--port", type=int, default=10090, help="listening port (default: 10090)"
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
