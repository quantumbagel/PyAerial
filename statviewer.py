import csv
import io
import sys

import pymongo
import ruamel.yaml
import readline  # Allows more intuitive inputs
from constants import *


def load_configuration() -> dict:
    """
    Load the configuration from config.yaml (or constants.CONFIG_FILE).
    :return: the data from the configuration file
    Will crash if file does not exist. This is intentional
    """
    yaml = ruamel.yaml.YAML()
    with open(CONFIG_FILE) as config:
        data = yaml.load(config)
    return data


def get_airplane_info(icao: str) -> dict | None:
    """
    Attempts to use the OpenSky Network to get more information about the ICAO
    :param icao: ICAO hex string
    :return: OpenSky Network airplane information
    """

    for line in open("database.csv"):
        if line.startswith(icao):
            plane = list(csv.reader(io.StringIO(line), quotechar="'"))[0]
            data = {}
            for ind, thing in enumerate(plane):
                data[STORE_OPENSKY_HEADER[ind]] = thing
            return data
    return None


def get_time_of_flight(collection: pymongo.collection.Collection) -> dict:
    collection.find_one()


VERBS = {"about": [], "status": [], "plane": [], "list": [str], "history": [], "help": [], "dump": [], "reset": [],
         "exit": []}
EXCLUDED_DATABASES = {"admin", "config", "local"}  # MongoDB's internal databases
configuration = load_configuration()

client = pymongo.MongoClient(configuration[CONFIG_GENERAL][CONFIG_GENERAL_MONGODB])

def verify_plane(pid):
    ret =  pid in client.list_database_names()
    if not ret:
        print(f"I don't know the plane \"{plane_id}\"!")
    return ret

def verify_flight(pid, fid):
    ret = fid in client.get_database(pid).list_database_names()
    if not ret:
        print(f"I don't know the flight id \"{fid}\"")
    return ret

help_text = \
    """
PyAerial StatViewer

help - display this help text
about - info about PyAerial
exit - close this terminal
reset - reset database or individual pieces of information (requires confirmation)
list - show summarized information
dump - show raw information
"""
print("Ready for user input.")

last_reset = False
reset_for = ""
while True:
    try:
        prompt = input("> ")
    except KeyboardInterrupt:
        print("\nlogout")
        sys.exit(0)
    prompt_breakdown = prompt.split()
    if not len(prompt_breakdown):
        continue
    verb = prompt_breakdown[0]

    if verb not in VERBS:
        print(f"[err] Invalid verb: {verb}")
        last_reset = False
        continue

    if verb == "about":
        print("PyAerial by Julian Reder (quantumbagel). Source: https://github.com/quantumbagel/PyAerial")
    elif verb == "status":
        size = client.admin.command('listDatabases')
        flights = 0
        for info in size['databases']:
            name = info['name']
            if name in EXCLUDED_DATABASES:
                continue
            db = client.get_database(name)
            flights += len(db.list_collection_names())
        print(f"Saved {len([i for i in size['databases'] if i['name'] not in EXCLUDED_DATABASES])} planes and {flights}"
              f" flights. Total size: {size['totalSize']}kb")
    elif verb == "list":
        if len(prompt_breakdown) == 1:
            print("[err] No argument supplied to command list!")
            continue
        first_argument = prompt_breakdown[1]
        if first_argument == "planes":
            size = client.admin.command('listDatabases')
            print(f"Planes ({len([i for i in size['databases'] if i['name'] not in EXCLUDED_DATABASES])}):"
                  f" {' '.join([d['name'] for d in size['databases'] if d['name'] not in EXCLUDED_DATABASES])}")
        elif first_argument == "flights":
            second_argument = prompt_breakdown[2]
            if not verify_plane(second_argument):
                continue
            flights = client.get_database(second_argument).list_collection_names()
            print(f"Flights for plane {second_argument} ({len(flights)}): {' '.join(flights)}")
        elif first_argument == "plane":
            plane_id = prompt_breakdown[2]
            planes = client.list_database_names()
            if plane_id not in planes:
                continue
            plane_db = client.get_database(plane_id)
            plane_db.list_collection_names()
            print("General data (most recent flight):")
            print(f"ICAO: {plane_id}")
            print("Callsign: not implemented")
            opensky = get_airplane_info(plane_id)
            print()
            if opensky is None:
                print("No OpenSky Network data!")
            else:
                print("OpenSky Network data:")
                print(f"Callsign: {opensky['callsign']}")
                print(f"Country: {opensky['country']}")
                print(f"Built: {opensky['built']}")
                print(f"Manufactured by: {opensky['manufacturer_icao']}/{opensky['manufacturer_name']}")
                print(f"Model: {opensky['model']}")
                print(f"Owner: {opensky['owner']}")
                # TODO: implement
            print()
            print("Calculated data:")
            print("Last Updated: not implemented")
            print("Most recent flight duration: not implemented")
            print("Packet Breakdown: not implemented")
        else:
            print(f"I don't know the argument \"{first_argument}\"!")
    elif verb == "reset":
        if len(prompt_breakdown) == 1:
            if not last_reset or reset_for != "":
                print("[confirmation] Are you sure you want to reset the database? Run \"reset\" again to reset.")
                last_reset = True
                reset_for = ""

            else:
                names = list([i for i in client.list_database_names() if i not in EXCLUDED_DATABASES])
                for database in names:
                    client.drop_database(database)
                print(f"[success] Database successfully reset. Dropped {len(names)} planes.")
                last_reset = False
        else:
            if not last_reset or reset_for != prompt_breakdown[1]:
                print(f"[confirmation] Are you sure you want to delete the plane {prompt_breakdown[1]}? Run \"reset\" again to reset.")
                last_reset = True
                reset_for = prompt_breakdown[1]
            else:
                names = list([i for i in client.list_database_names() if i not in EXCLUDED_DATABASES])
                client.drop_database(prompt_breakdown[1])
                print(f"[success] Dropped plane {prompt_breakdown[1]}.")
                last_reset = False
                reset_for = ""
    elif verb == "exit":
        print("logout")  # Homage to linux :)
        sys.exit(0)
    elif verb == "help":
        print(help_text)
    elif verb == "dump":
        print("Outputting raw JSON. Please put in a JSON viewer.")
        first_argument = prompt_breakdown[1]
        data = {}
        planes = client.list_database_names()
        if first_argument == "plane":
            plane_id = prompt_breakdown[2]
            db = client.get_database(plane_id)
            data[plane_id] = {}
            for flight_id in db.list_collection_names():
                data[plane_id][flight_id] = {}
                flight = db.get_collection(flight_id)
                flight_contents = list(flight.find({}, {"_id": 0}))
                for flight_content in flight_contents:
                    category = flight_content[STORAGE_CATEGORY]
                    if category == STORE_INFO:
                        data[plane_id][flight_id][STORE_INFO] = flight_content
                    else:
                        data_type = flight_content[STORAGE_DATA_TYPE]
                        if category not in data[plane_id][flight_id].keys():
                            data[plane_id][flight_id][category] = {}
                        data[plane_id][flight_id][category][data_type] = flight_content
            print(data)
        elif first_argument == "flight":
            plane_id = prompt_breakdown[2]
            flight_id = prompt_breakdown[3]
            data[plane_id] = {}
            data[plane_id][flight_id] = {}
            db = client.get_database(plane_id)
            flight = db.get_collection(flight_id)
            if not verify_flight(plane_id, flight_id):
                continue
            flight_contents = list(flight.find({}, {"_id": 0}))
            for flight_content in flight_contents:
                category = flight_content[STORAGE_CATEGORY]
                if category == STORE_INFO:
                    data[plane_id][flight_id][STORE_INFO] = flight_content
                else:
                    data_type = flight_content[STORAGE_DATA_TYPE]
                    if category not in data[plane_id][flight_id].keys():
                        data[plane_id][flight_id][category] = {}
                    data[plane_id][flight_id][category][data_type] = flight_content
            print(data)
        elif first_argument == "all":
            print("Please note that dumping ALL data can take a while.")
            for plane_id in client.list_database_names():
                if plane_id in EXCLUDED_DATABASES:
                    continue
                db = client.get_database(plane_id)
                data[plane_id] = {}
                for flight_id in db.list_collection_names():
                    data[plane_id][flight_id] = {}
                    flight = db.get_collection(flight_id)
                    flight_contents = list(flight.find({}, {"_id": 0}))
                    for flight_content in flight_contents:
                        category = flight_content[STORAGE_CATEGORY]
                        if category == STORE_INFO:
                            data[plane_id][flight_id][STORE_INFO] = flight_content
                        else:
                            data_type = flight_content[STORAGE_DATA_TYPE]
                            if category not in data[plane_id][flight_id].keys():
                                data[plane_id][flight_id][category] = {}
                            data[plane_id][flight_id][category][data_type] = flight_content
            print(data)
        elif first_argument == "opensky":
            plane = prompt_breakdown[2]
            if not verify_plane(plane):
                continue
            print(get_airplane_info(plane))
            pass
        else:
            print(f"[err] I don't know the keyword {first_argument}")
    if verb != "reset":
        last_reset = False
