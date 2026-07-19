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
    client = pymongo.MongoClient(config.database.uri)
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
    try:
        db = client.get_default_database()
    except Exception:
        db = client.get_database("pyaerial")

    planes = len(db.get_collection("flights").distinct("icao"))
    flights = db.get_collection("flights").count_documents({})
    
    try:
        stats = db.command("dbStats")
        total_size = stats.get("dataSize", 0)
    except Exception:
        total_size = 0

    print(f"Saved {planes} plane(s) and {flights} flight(s). "
          f"Total data size: {total_size} bytes")


def _verify_plane(client: pymongo.MongoClient, plane_id: str) -> bool:
    try:
        db = client.get_default_database()
    except Exception:
        db = client.get_database("pyaerial")

    record = db.get_collection("flights").find_one({"icao": plane_id.lower()})
    if record is not None:
        return True
    print(f"I don't know the plane {plane_id!r}!")
    return False


def _verify_flight(client: pymongo.MongoClient, plane_id: str, flight_id: str) -> bool:
    try:
        db = client.get_default_database()
    except Exception:
        db = client.get_database("pyaerial")

    record = db.get_collection("flights").find_one({"icao": plane_id.lower(), "_id": flight_id})
    if record is not None:
        return True
    print(f"I don't know the flight id {flight_id!r}")
    return False


def _cmd_list(client: pymongo.MongoClient, parts: list[str],
              aircraft_db: AircraftDB) -> None:
    if len(parts) < 2:
        print("[err] No argument supplied to command list!")
        return

    try:
        db = client.get_default_database()
    except Exception:
        db = client.get_database("pyaerial")

    arg = parts[1]
    if arg == "planes":
        names = db.get_collection("flights").distinct("icao")
        print(f"Planes ({len(names)}): {' '.join(names)}")
    elif arg == "flights":
        if len(parts) < 3:
            print("[err] list flights requires a plane id")
            return
        plane_id = parts[2]
        if not _verify_plane(client, plane_id):
            return
        cursor = db.get_collection("flights").find({"icao": plane_id.lower()}, {"_id": 1})
        flights = [doc["_id"] for doc in cursor]
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
        record = aircraft_db.lookup_cached(plane_id)
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
    try:
        db = client.get_default_database()
    except Exception:
        db = client.get_database("pyaerial")

    if len(parts) == 1:
        if not last_reset or reset_for:
            print('[confirmation] Are you sure you want to reset the database? '
                  'Run "reset" again to confirm.')
            return True, ""
        db.drop_collection("flights")
        db.drop_collection("telemetry")
        db.drop_collection("alerts")
        
        # Recreate indexes
        db.get_collection("flights").create_index([("icao", pymongo.ASCENDING)])
        db.get_collection("flights").create_index([("status", pymongo.ASCENDING)])
        db.get_collection("telemetry").create_index([
            ("flight_id", pymongo.ASCENDING),
            ("timestamp", pymongo.ASCENDING)
        ])
        db.get_collection("telemetry").create_index([
            ("icao", pymongo.ASCENDING),
            ("timestamp", pymongo.ASCENDING)
        ])
        db.get_collection("telemetry").create_index([("position", pymongo.GEOSPHERE)])
        db.get_collection("alerts").create_index([("timestamp", pymongo.DESCENDING)])
        db.get_collection("alerts").create_index([("flight_id", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)])
        
        print("[success] Database reset. Dropped all planes and flights.")
        return False, ""

    target = parts[1].lower()
    if not last_reset or reset_for != target:
        print(f'[confirmation] Delete plane {target}? Run "reset {target}" again to confirm.')
        return True, target
    
    db.get_collection("flights").delete_many({"icao": target})
    db.get_collection("telemetry").delete_many({"icao": target})
    db.get_collection("alerts").delete_many({"icao": target})
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
        record = aircraft_db.lookup_cached(plane)
        print(json.dumps(record, indent=2) if record else "No record found.")
        return

    if arg == "all":
        print("Dumping all data (this may take a while)...")
        try:
            db = client.get_default_database()
        except Exception:
            db = client.get_database("pyaerial")
        
        data = {}
        distinct_planes = db.get_collection("flights").distinct("icao")
        for plane_id in distinct_planes:
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
    try:
        db = client.get_default_database()
    except Exception:
        db = client.get_database("pyaerial")

    cursor = db.get_collection("flights").find({"icao": plane_id.lower()}, {"_id": 1})
    flight_ids = [doc["_id"] for doc in cursor]
    return {fid: _dump_flight(client, plane_id, fid) for fid in flight_ids}


def _dump_flight(client: pymongo.MongoClient, plane_id: str, flight_id: str) -> dict:
    try:
        db = client.get_default_database()
    except Exception:
        db = client.get_database("pyaerial")

    flight_doc = db.get_collection("flights").find_one({"_id": flight_id})
    if not flight_doc:
        return {}

    telemetry_cursor = db.get_collection("telemetry").find({"flight_id": flight_id}).sort("timestamp", pymongo.ASCENDING)
    
    from pyaerial.constants import STORE_LAT, STORE_LONG, STORE_RECV_DATA, STORE_CALC_DATA
    
    series_data = {}
    for doc in telemetry_cursor:
        t = doc["timestamp"]
        
        # If position GeoJSON exists, reconstruct latitude and longitude
        if "position" in doc and doc["position"].get("type") == "Point":
            coords = doc["position"].get("coordinates", [])
            if len(coords) == 2:
                series_data.setdefault("longitude", []).append([t, coords[0]])
                series_data.setdefault("latitude", []).append([t, coords[1]])
                
        # Process other fields in the telemetry document
        for k, v in doc.items():
            if k not in ("_id", "flight_id", "icao", "timestamp", "position"):
                field = k
                if k == "horizontal_speed":
                    field = "horizontal_speed"
                elif k == "direction":
                    field = "direction"
                series_data.setdefault(field, []).append([t, v])

    result: dict = {}
    for field, data_points in series_data.items():
        if field in ("latitude", "longitude", "altitude", "vertical_speed"):
            category = STORE_RECV_DATA
        else:
            category = STORE_CALC_DATA
            
        result.setdefault(category, {})
        result[category][field] = {
            "category": category,
            "type": field,
            "data": data_points
        }

    info_doc = {
        "category": "info",
        "zone": flight_doc.get("zone"),
        "level": flight_doc.get("level"),
    }
    if "info" in flight_doc:
        info_doc.update(flight_doc["info"])
    if "internal" in flight_doc:
        info_doc.update(flight_doc["internal"])
        
    result[STORE_INFO] = info_doc
    return result
