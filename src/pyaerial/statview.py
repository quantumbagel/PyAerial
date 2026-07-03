"""
Interactive MongoDB browser for saved PyAerial flights.

Port of the original ``statviewer.py`` with logging instead of bare prints and
support for the v2 config schema.
"""
from __future__ import annotations

import json
import logging
import sys

import pymongo

from pyaerial.calc.aircraft_db import AircraftDB
from pyaerial.config import load_config
from pyaerial.constants import (
    DEFAULT_AIRCRAFT_DB,
    STORAGE_CATEGORY,
    STORAGE_DATA_TYPE,
    STORE_INFO,
)

log = logging.getLogger("pyaerial.statview")

EXCLUDED_DATABASES = {"admin", "config", "local"}

HELP_TEXT = """
PyAerial StatViewer

help   - display this help text
about  - info about PyAerial
exit   - close this terminal
reset  - reset database or individual planes (requires confirmation)
list   - show summarized information (planes, flights, plane)
dump   - show raw information (plane, flight, all, opensky)
status - database size summary
""".strip()


def run_statview(config_path: str = "config.yaml", *,
                 aircraft_db_path: str = DEFAULT_AIRCRAFT_DB) -> None:
    config = load_config(config_path)
    client = pymongo.MongoClient(config.general.mongodb)
    aircraft_db = AircraftDB(aircraft_db_path)

    print("Ready for user input.")
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

        verb = parts[0]
        if verb not in {"about", "status", "plane", "list", "history", "help",
                        "dump", "reset", "exit"}:
            print(f"[err] Invalid verb: {verb}")
            last_reset = False
            continue

        if verb == "about":
            print("PyAerial by Julian Reder (quantumbagel). "
                  "Source: https://github.com/quantumbagel/PyAerial")
        elif verb == "status":
            _cmd_status(client)
        elif verb == "list":
            _cmd_list(client, parts, aircraft_db)
        elif verb == "reset":
            last_reset, reset_for = _cmd_reset(client, parts, last_reset, reset_for)
        elif verb == "exit":
            print("logout")
            return
        elif verb == "help":
            print(HELP_TEXT)
        elif verb == "dump":
            _cmd_dump(client, parts, aircraft_db)

        if verb != "reset":
            last_reset = False


def _cmd_status(client: pymongo.MongoClient) -> None:
    size = client.admin.command("listDatabases")
    planes = 0
    flights = 0
    for info in size["databases"]:
        name = info["name"]
        if name in EXCLUDED_DATABASES:
            continue
        planes += 1
        flights += len(client.get_database(name).list_collection_names())
    print(f"Saved {planes} plane(s) and {flights} flight(s). "
          f"Total size: {size['totalSize']} bytes")


def _verify_plane(client: pymongo.MongoClient, plane_id: str) -> bool:
    if plane_id in client.list_database_names():
        return True
    print(f"I don't know the plane {plane_id!r}!")
    return False


def _verify_flight(client: pymongo.MongoClient, plane_id: str, flight_id: str) -> bool:
    if flight_id in client.get_database(plane_id).list_collection_names():
        return True
    print(f"I don't know the flight id {flight_id!r}")
    return False


def _cmd_list(client: pymongo.MongoClient, parts: list[str],
              aircraft_db: AircraftDB) -> None:
    if len(parts) < 2:
        print("[err] No argument supplied to command list!")
        return

    arg = parts[1]
    if arg == "planes":
        names = [d for d in client.list_database_names() if d not in EXCLUDED_DATABASES]
        print(f"Planes ({len(names)}): {' '.join(names)}")
    elif arg == "flights":
        if len(parts) < 3:
            print("[err] list flights requires a plane id")
            return
        plane_id = parts[2]
        if not _verify_plane(client, plane_id):
            return
        flights = client.get_database(plane_id).list_collection_names()
        print(f"Flights for plane {plane_id} ({len(flights)}): {' '.join(flights)}")
    elif arg == "plane":
        if len(parts) < 3:
            print("[err] list plane requires a plane id")
            return
        plane_id = parts[2]
        if not _verify_plane(client, plane_id):
            return
        print("General data:")
        print(f"ICAO: {plane_id}")
        record = aircraft_db.lookup(plane_id)
        if record is None:
            print("No aircraft metadata in local database.")
        else:
            print(f"Callsign: {record.get('callsign', 'n/a')}")
            print(f"Country: {record.get('country', 'n/a')}")
            print(f"Built: {record.get('built', 'n/a')}")
            print(f"Manufacturer: {record.get('manufacturer_name', 'n/a')}")
            print(f"Model: {record.get('model', 'n/a')}")
            print(f"Owner: {record.get('owner', 'n/a')}")
    else:
        print(f"I don't know the argument {arg!r}!")


def _cmd_reset(client: pymongo.MongoClient, parts: list[str],
               last_reset: bool, reset_for: str) -> tuple[bool, str]:
    if len(parts) == 1:
        if not last_reset or reset_for:
            print('[confirmation] Are you sure you want to reset the database? '
                  'Run "reset" again to confirm.')
            return True, ""
        names = [d for d in client.list_database_names() if d not in EXCLUDED_DATABASES]
        for database in names:
            client.drop_database(database)
        print(f"[success] Database reset. Dropped {len(names)} plane(s).")
        return False, ""

    target = parts[1]
    if not last_reset or reset_for != target:
        print(f'[confirmation] Delete plane {target}? Run "reset {target}" again to confirm.')
        return True, target
    client.drop_database(target)
    print(f"[success] Dropped plane {target}.")
    return False, ""


def _cmd_dump(client: pymongo.MongoClient, parts: list[str],
              aircraft_db: AircraftDB) -> None:
    if len(parts) < 2:
        print("[err] dump requires a subcommand (plane, flight, all, opensky)")
        return

    arg = parts[1]
    if arg == "opensky":
        if len(parts) < 3:
            print("[err] dump opensky requires a plane id")
            return
        plane = parts[2]
        if not _verify_plane(client, plane):
            return
        record = aircraft_db.lookup(plane)
        print(json.dumps(record, indent=2) if record else "No record found.")
        return

    if arg == "all":
        print("Dumping all data (this may take a while)...")
        data = {}
        for plane_id in client.list_database_names():
            if plane_id in EXCLUDED_DATABASES:
                continue
            data[plane_id] = _dump_plane(client, plane_id)
        print(json.dumps(data, indent=2, default=str))
        return

    if arg == "plane":
        if len(parts) < 3:
            print("[err] dump plane requires a plane id")
            return
        plane_id = parts[2]
        if not _verify_plane(client, plane_id):
            return
        print(json.dumps({plane_id: _dump_plane(client, plane_id)}, indent=2, default=str))
        return

    if arg == "flight":
        if len(parts) < 4:
            print("[err] dump flight requires plane id and flight id")
            return
        plane_id, flight_id = parts[2], parts[3]
        if not _verify_plane(client, plane_id) or not _verify_flight(client, plane_id, flight_id):
            return
        print(json.dumps({plane_id: {flight_id: _dump_flight(client, plane_id, flight_id)}},
                         indent=2, default=str))
        return

    print(f"[err] Unknown dump subcommand {arg!r}")


def _dump_plane(client: pymongo.MongoClient, plane_id: str) -> dict:
    db = client.get_database(plane_id)
    return {fid: _dump_flight(client, plane_id, fid) for fid in db.list_collection_names()}


def _dump_flight(client: pymongo.MongoClient, plane_id: str, flight_id: str) -> dict:
    flight = client.get_database(plane_id).get_collection(flight_id)
    result: dict = {}
    for doc in flight.find({}, {"_id": 0}):
        category = doc[STORAGE_CATEGORY]
        if category == STORE_INFO:
            result[STORE_INFO] = doc
        else:
            data_type = doc[STORAGE_DATA_TYPE]
            result.setdefault(category, {})
            result[category][data_type] = doc
    return result
