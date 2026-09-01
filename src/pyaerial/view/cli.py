"""
Interactive flight viewer REPL for PyAerial.

Command implementations live in :mod:`pyaerial.view.commands`.
"""

from __future__ import annotations

from typing import Any

import pymongo

from pyaerial.constants import DEFAULT_AIRCRAFT_DB
from pyaerial.config import load_config
from pyaerial.enrich.aircraft_db import AircraftDB
from pyaerial.view.commands import cmd_dump, cmd_list, cmd_reset, cmd_status
from pyaerial.view.db import set_view_db_name
from pyaerial.view.live_display import run_live_loop
from pyaerial.view.store import open_live_session

HELP_TEXT = """
PyAerial Flight Viewer

help      - display this help text
about     - info about PyAerial
exit      - close this terminal
reset     - reset database or individual planes (requires confirmation)
list      - show summarized information (planes, flights, plane <id>)
dump      - show raw information (plane <id>, flight <id>, live, all, aircraft <icao>)
status    - database and live stream summary
live      - start live flight display
""".strip()


def run_view(
    config_path: str = "config.yaml",
    *,
    aircraft_db_path: str = DEFAULT_AIRCRAFT_DB,
    mock: bool = False,
) -> None:
    """Run interactive flight viewer command-line session."""
    config = load_config(config_path)
    set_view_db_name(config.database.name)
    aircraft_db = AircraftDB(aircraft_db_path)
    live_store, engine = open_live_session(
        config, mock=mock, aircraft_db_path=aircraft_db_path
    )

    client: pymongo.MongoClient | None = None
    if not mock:
        try:
            client = pymongo.MongoClient(
                config.database.uri, serverSelectionTimeoutMS=2000
            )
        except Exception:
            client = None

    print("Ready for user input.")
    try:
        _run_view_loop(client, aircraft_db, live_store)
    finally:
        if engine is not None:
            engine.shutdown()
        else:
            live_store.close()
        aircraft_db.close()
        if client is not None:
            client.close()


def _run_view_loop(
    client: pymongo.MongoClient | None,
    aircraft_db: AircraftDB,
    live_store: Any,
) -> None:
    last_reset = False
    reset_for = ""

    while True:
        try:
            prompt = input("> ")
        except (KeyboardInterrupt, EOFError):
            print("\nlogout")
            return

        parts = prompt.split()
        if not parts:
            continue

        verb = parts[0].lower()
        if verb not in {
            "about",
            "status",
            "plane",
            "list",
            "history",
            "help",
            "dump",
            "reset",
            "exit",
            "live",
        }:
            print(f"[err] Invalid verb: {verb}")
            last_reset = False
            continue

        if verb in {"plane", "history"}:
            print(f"[err] '{verb}' is not a command. Try 'list planes' or 'help'.")
            last_reset = False
            continue

        if verb == "about":
            print(
                "PyAerial by Julian Reder (quantumbagel). "
                "Source: https://github.com/quantumbagel/PyAerial"
            )
        elif verb == "status":
            cmd_status(client, live_store=live_store)
        elif verb == "list":
            cmd_list(client, parts, aircraft_db, live_store=live_store)
        elif verb == "reset":
            last_reset, reset_for = cmd_reset(
                client, parts, last_reset, reset_for, live_store=live_store
            )
        elif verb == "exit":
            print("logout")
            return
        elif verb == "help":
            print(HELP_TEXT)
        elif verb == "dump":
            cmd_dump(client, parts, aircraft_db, live_store=live_store)
        elif verb == "live":
            try:
                run_live_loop(live_store)
            except KeyboardInterrupt:
                print("\n[live] Stopped.")

        if verb != "reset":
            last_reset = False
