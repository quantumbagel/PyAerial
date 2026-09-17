"""
Interactive flight viewer REPL for PyAerial.

Command implementations live in :mod:`pyaerial.view.commands`.
"""

from __future__ import annotations

from typing import Any

from pyaerial.constants import DEFAULT_AIRCRAFT_DB
from pyaerial.config import load_config
from pyaerial.enrich.aircraft_db import AircraftDB
from pyaerial.store.history import HistoryStore
from pyaerial.view.commands import cmd_dump, cmd_list, cmd_reset, cmd_status
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
) -> None:
    """Run interactive flight viewer command-line session."""
    config = load_config(config_path)
    aircraft_db = AircraftDB(aircraft_db_path)
    live_store = open_live_session(config)
    history = HistoryStore(config.database.path)

    print("Ready for user input.")
    try:
        _run_view_loop(history, aircraft_db, live_store)
    finally:
        live_store.close()
        aircraft_db.close()
        history.close()


def _run_view_loop(
    history: HistoryStore | None,
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
            "list",
            "help",
            "dump",
            "reset",
            "exit",
            "live",
        }:
            print(f"[err] Invalid verb: {verb}")
            last_reset = False
            continue

        if verb == "about":
            print(
                "PyAerial by Julian Reder (quantumbagel). "
                "Source: https://github.com/quantumbagel/PyAerial"
            )
        elif verb == "status":
            cmd_status(history, live_store=live_store)
        elif verb == "list":
            cmd_list(history, parts, aircraft_db, live_store=live_store)
        elif verb == "reset":
            last_reset, reset_for = cmd_reset(
                history, parts, last_reset, reset_for, live_store=live_store
            )
        elif verb == "exit":
            print("logout")
            return
        elif verb == "help":
            print(HELP_TEXT)
        elif verb == "dump":
            cmd_dump(history, parts, aircraft_db, live_store=live_store)
        elif verb == "live":
            try:
                run_live_loop(live_store)
            except KeyboardInterrupt:
                print("\n[live] Stopped.")

        if verb != "reset":
            last_reset = False
