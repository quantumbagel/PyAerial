"""
Interactive MongoDB flight browser for PyAerial.
"""
from __future__ import annotations

import sys

import pymongo
import readline  # noqa: F401  # nicer input()

from pyaerial.config import load_configuration, resolve_config_path
from pyaerial.constants import (
    CONFIG_FILE,
    CONFIG_GENERAL,
    CONFIG_GENERAL_MONGODB,
    STORAGE_CATEGORY,
    STORAGE_DATA_TYPE,
    STORE_FIRST_PACKET,
    STORE_INFO,
    STORE_INTERNAL,
    STORE_MOST_RECENT_PACKET,
    STORE_PACKET_TYPE,
    STORE_CALLSIGN,
)
from pyaerial.opensky import load_airplane_info


EXCLUDED_DATABASES = {"admin", "config", "local"}

VERBS = {
    "about",
    "status",
    "plane",
    "list",
    "history",
    "help",
    "dump",
    "reset",
    "exit",
}

HELP_TEXT = """
PyAerial StatViewer

help - display this help text
about - info about PyAerial
exit - close this terminal
reset - reset database or individual pieces of information (requires confirmation)
list - show summarized information (list planes | list flights <icao> | list plane <icao>)
dump - show raw information (dump plane <icao> | dump flight <icao> <coll> | dump all | dump opensky <icao>)
"""


def verify_plane(client: pymongo.MongoClient, pid: str) -> bool:
    ret = pid in client.list_database_names()
    if not ret:
        print(f'I don\'t know the plane "{pid}"!')
    return ret


def verify_flight(client: pymongo.MongoClient, pid: str, fid: str) -> bool:
    ret = fid in client.get_database(pid).list_collection_names()
    if not ret:
        print(f'I don\'t know the flight id "{fid}"')
    return ret


def _latest_flight_collection(db: pymongo.database.Database) -> str | None:
    names = db.list_collection_names()
    if not names:
        return None

    def sort_key(n: str):
        part = n.split("-", 1)[0]
        try:
            return int(part)
        except ValueError:
            return 0

    return max(names, key=sort_key)


def _summarize_recent_flight(client: pymongo.MongoClient, plane_id: str) -> None:
    db = client.get_database(plane_id)
    coll_name = _latest_flight_collection(db)
    if not coll_name:
        print("No saved flights for this plane.")
        return
    flight = db.get_collection(coll_name)
    docs = list(flight.find({}, {"_id": 0}))
    info_doc = next((d for d in docs if d.get(STORAGE_CATEGORY) == STORE_INFO), None)
    print(f"Most recent flight collection: {coll_name}")
    if info_doc:
        if STORE_CALLSIGN in info_doc:
            print(f"Callsign (from saved info): {info_doc[STORE_CALLSIGN]}")
        zi = info_doc.get(STORE_INFO, {})
        if isinstance(zi, dict) and STORE_CALLSIGN in zi:
            print(f"Callsign (nested info): {zi[STORE_CALLSIGN]}")
        intl = info_doc.get(STORE_INTERNAL, {})
        if isinstance(intl, dict):
            if STORE_MOST_RECENT_PACKET in intl:
                print(f"Last update (epoch): {intl[STORE_MOST_RECENT_PACKET]}")
            if STORE_FIRST_PACKET in intl:
                print(f"First packet (epoch): {intl[STORE_FIRST_PACKET]}")
            if STORE_PACKET_TYPE in intl:
                print(f"Packet type counts: {intl[STORE_PACKET_TYPE]}")


def run(config_path: str | None = None) -> None:
    path = resolve_config_path(config_path)
    configuration = load_configuration(path)
    client = pymongo.MongoClient(configuration[CONFIG_GENERAL][CONFIG_GENERAL_MONGODB])

    print("Ready for user input.")
    last_reset = False
    reset_for = ""

    while True:
        try:
            prompt = input("> ")
        except (KeyboardInterrupt, EOFError):
            print("\nlogout")
            sys.exit(0)

        parts = prompt.split()
        if not parts:
            continue
        verb = parts[0]

        if verb not in VERBS:
            print(f"[err] Invalid verb: {verb}")
            last_reset = False
            continue

        if verb == "about":
            print("PyAerial by Julian Reder (quantumbagel). Source: https://github.com/quantumbagel/PyAerial")
        elif verb == "status":
            size = client.admin.command("listDatabases")
            flights = 0
            for info in size["databases"]:
                name = info["name"]
                if name in EXCLUDED_DATABASES:
                    continue
                flights += len(client.get_database(name).list_collection_names())
            nplanes = len(
                [i for i in size["databases"] if i["name"] not in EXCLUDED_DATABASES]
            )
            print(f"Saved {nplanes} planes and {flights} flights. Total size: {size['totalSize']} bytes")
        elif verb == "list":
            if len(parts) == 1:
                print("[err] No argument supplied to command list!")
                continue
            first_argument = parts[1]
            if first_argument == "planes":
                size = client.admin.command("listDatabases")
                names = [d["name"] for d in size["databases"] if d["name"] not in EXCLUDED_DATABASES]
                print(f"Planes ({len(names)}): {' '.join(names)}")
            elif first_argument == "flights":
                if len(parts) < 3:
                    print("[err] Usage: list flights <icao>")
                    continue
                icao = parts[2]
                if not verify_plane(client, icao):
                    continue
                flights = client.get_database(icao).list_collection_names()
                print(f"Flights for plane {icao} ({len(flights)}): {' '.join(flights)}")
            elif first_argument == "plane":
                if len(parts) < 3:
                    print("[err] Usage: list plane <icao>")
                    continue
                plane_id = parts[2]
                if plane_id not in client.list_database_names():
                    print(f'[err] Unknown plane "{plane_id}"')
                    continue
                print("General data (most recent flight):")
                print(f"ICAO: {plane_id}")
                opensky = load_airplane_info(plane_id.upper())
                _summarize_recent_flight(client, plane_id)
                print()
                if opensky is None:
                    print("No OpenSky CSV data (set PYAERIAL_OPENSKY_CSV or place database.csv in cwd).")
                else:
                    print("OpenSky CSV data:")
                    print(f"Callsign: {opensky.get('callsign')}")
                    print(f"Country: {opensky.get('country')}")
                    print(f"Built: {opensky.get('built')}")
                    print(
                        f"Manufactured by: {opensky.get('manufacturer_icao')}/"
                        f"{opensky.get('manufacturer_name')}"
                    )
                    print(f"Model: {opensky.get('model')}")
                    print(f"Owner: {opensky.get('owner')}")
            else:
                print(f'[err] Unknown list subcommand "{first_argument}"')
        elif verb == "reset":
            if len(parts) == 1:
                if not last_reset or reset_for != "":
                    print('[confirmation] Reset entire database? Run "reset" again to confirm.')
                    last_reset = True
                    reset_for = ""
                else:
                    names = [i for i in client.list_database_names() if i not in EXCLUDED_DATABASES]
                    for database in names:
                        client.drop_database(database)
                    print(f"[success] Dropped {len(names)} plane databases.")
                    last_reset = False
            else:
                target = parts[1]
                if not last_reset or reset_for != target:
                    print(
                        f'[confirmation] Drop plane "{target}"? Run "reset {target}" again to confirm.'
                    )
                    last_reset = True
                    reset_for = target
                else:
                    client.drop_database(target)
                    print(f"[success] Dropped plane {target}.")
                    last_reset = False
                    reset_for = ""
        elif verb == "exit":
            print("logout")
            sys.exit(0)
        elif verb == "help":
            print(HELP_TEXT)
        elif verb == "dump":
            if len(parts) < 2:
                print("[err] dump needs a subcommand")
                continue
            first_argument = parts[1]
            data = {}
            if first_argument == "plane":
                if len(parts) < 3:
                    print("[err] Usage: dump plane <icao>")
                    continue
                plane_id = parts[2]
                db = client.get_database(plane_id)
                data[plane_id] = {}
                for flight_id in db.list_collection_names():
                    data[plane_id][flight_id] = {}
                    flight = db.get_collection(flight_id)
                    for flight_content in flight.find({}, {"_id": 0}):
                        category = flight_content[STORAGE_CATEGORY]
                        if category == STORE_INFO:
                            data[plane_id][flight_id][STORE_INFO] = flight_content
                        else:
                            data_type = flight_content[STORAGE_DATA_TYPE]
                            data[plane_id][flight_id].setdefault(category, {})
                            data[plane_id][flight_id][category][data_type] = flight_content
                print(data)
            elif first_argument == "flight":
                if len(parts) < 4:
                    print("[err] Usage: dump flight <icao> <collection>")
                    continue
                plane_id = parts[2]
                flight_id = parts[3]
                if not verify_flight(client, plane_id, flight_id):
                    continue
                data[plane_id] = {flight_id: {}}
                flight = client.get_database(plane_id).get_collection(flight_id)
                for flight_content in flight.find({}, {"_id": 0}):
                    category = flight_content[STORAGE_CATEGORY]
                    if category == STORE_INFO:
                        data[plane_id][flight_id][STORE_INFO] = flight_content
                    else:
                        data_type = flight_content[STORAGE_DATA_TYPE]
                        data[plane_id][flight_id].setdefault(category, {})
                        data[plane_id][flight_id][category][data_type] = flight_content
                print(data)
            elif first_argument == "all":
                print("Dumping all plane data (may take a while).")
                for plane_id in client.list_database_names():
                    if plane_id in EXCLUDED_DATABASES:
                        continue
                    db = client.get_database(plane_id)
                    data[plane_id] = {}
                    for flight_id in db.list_collection_names():
                        data[plane_id][flight_id] = {}
                        flight = db.get_collection(flight_id)
                        for flight_content in flight.find({}, {"_id": 0}):
                            category = flight_content[STORAGE_CATEGORY]
                            if category == STORE_INFO:
                                data[plane_id][flight_id][STORE_INFO] = flight_content
                            else:
                                data_type = flight_content[STORAGE_DATA_TYPE]
                                data[plane_id][flight_id].setdefault(category, {})
                                data[plane_id][flight_id][category][data_type] = flight_content
                print(data)
            elif first_argument == "opensky":
                if len(parts) < 3:
                    print("[err] Usage: dump opensky <icao>")
                    continue
                plane = parts[2]
                if not verify_plane(client, plane):
                    continue
                print(load_airplane_info(plane.upper()))
            else:
                print(f"[err] Unknown dump keyword {first_argument}")
        elif verb == "history":
            print("[err] history is not implemented yet")

        if verb != "reset":
            last_reset = False


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(prog="pyaerial-statviewer")
    p.add_argument("-c", "--config", default=None, help=f"Config path (default: $PYAERIAL_CONFIG or ./{CONFIG_FILE})")
    args = p.parse_args()
    run(args.config)


if __name__ == "__main__":
    main()
