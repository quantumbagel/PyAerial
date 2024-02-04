import threading
import time
import pyModeS as pms
import signal_generator
from signal_generator import message_queue as messages


def initate_signal_generator():
    signal_thread = threading.Thread(target=signal_generator.run, daemon=True)
    signal_thread.start()
    return signal_thread


lat_h = 35.6810752
lon_h = -78.8758528
generator_thread = initate_signal_generator()

categories = {2: {1: "Surface Emergency Vehicle", 3: "Surface Service Vehicle", 4: "Ground Obstruction (4)",
                  5: "Ground Obstruction (5)", 6: "Ground Obstruction (6)", 7: "Ground Obstruction (7)"},
              3: {1: "Glider/Sailplane", 2: "Lighter-than-air", 3: "Parachutist/Skydiver",
                  4: "Ultralight/Hang-glider/paraglider", 6: "UAV (unmanned aerial vehicle)",
                  7: "Space/transatmospheric vehicle"},
              4: {1: "Light (<7000kg)", 2: "Medium 1 (7000 to 34000kg)", 3: "Medium 2 (34000 to 136000kg)",
                  4: "High vortex aircraft", 5: "Heavy (>13600kg)",
                  6: "High performance (>5g) and high speed (>740km/h)", 7: "Rotorcraft (helicopter)"}}


def classify(msg):
    """
    Classify an ADS-B message
    ASSUMES downlink=17 or 18
    :param msg: the message to classify
    :return: some data ig
    """
    typecode = pms.typecode(msg)
    if typecode == -1:
        print(f"Invalid ADS-B message, ignoring. {msg}")
        return
    data = None
    icao = pms.icao(msg)
    typecode_category = "Unknown"
    if 1 <= typecode <= 4:  # Aircraft identification
        ca = pms.adsb.category(msg)
        data = {"info": {"icao": icao, "callsign": pms.adsb.callsign(msg).replace("_", ""),
                         "category": [typecode, ca]}, "data": {}}
        typecode_category = 1

    elif 5 <= typecode <= 8:  # Surface position
        lat, lon = pms.adsb.position_with_ref(msg, lat_h, lon_h)
        speed, angle, vert_rate, speed_type, angle_source, vert_rate_source = pms.adsb.velocity(msg, source=True)
        data = {"info": {"icao": icao}, "data": {"latitude": lat, "longitude": lon, "horizontal_speed": speed * 1.852,
                                                 "direction": angle, "vertical_speed": vert_rate * 0.018288}}
        typecode_category = 2

    elif 9 <= typecode <= 18 or 20 <= typecode <= 22:  # Airbone position (baro alt/GNSS alt)
        lat, lon = pms.adsb.position_with_ref(msg, lat_h, lon_h)
        alt = pms.adsb.altitude(msg)
        data = {"info": {"icao": icao}, "data": {"latitude": lat, "longitude": lon, "altitude": alt}}
        if 9 <= typecode <= 18:
            typecode_category = 3
        else:
            typecode_category = 4

    elif typecode == 19:  # Airbone velocities
        speed, angle, vert_rate, speed_type, angle_source, vert_rate_source = pms.adsb.velocity(msg, source=True)
        data = {"info": {"icao": icao},
                "data": {"horizontal_speed": speed * 1.852, "direction": angle, "vertical_speed": vert_rate * 0.018288}}
        typecode_category = 5

    elif typecode == 28:  # Aircraft status
        pass
    elif typecode == 29:  # Target state and status information
        pass
    elif typecode == 31:  # Aircraft operation status
        pass  # Not going to implement this type of message

    return data, typecode_category


def get_newest(packets):
    return packets[-1]


def check_generator_thread():
    global generator_thread
    if not generator_thread.is_alive():
        print("Generator thread has died! Waiting 3 seconds to restart...")
        time.sleep(3)  # Wait it out
        print("Restarting generator thread...")
        generator_thread = initate_signal_generator()

planes = {}
processed_messages = 0
while True:
    check_generator_thread()
    messages_copy = messages[:]
    for message in messages_copy:
        data, typecode_cat = classify(message[0])
        if data is None:  # Not implemented yet :(
            continue
        processed_messages += 1
        if data["info"]["icao"] not in planes.keys():  # Do we have this plane in our current tracker?
            planes[data["info"]["icao"]] = data  # We don't, so just format the data and insert it
            for item in planes[data["info"]["icao"]]["data"]:
                c_item = planes[data["info"]["icao"]]["data"][item]
                planes[data["info"]["icao"]]["data"][item] = [[c_item, message[1]]]
        else:  # We do, so find and replace (or update) data
            current_info = planes[data["info"]["icao"]]["info"]  # Current plane info
            my_info = data["info"]
            for item in my_info.keys():  # Find and replace/update data
                if item not in current_info.keys():
                    current_info.update({item: my_info[item]})
                else:
                    current_info[item] = my_info[item]
            planes[data["info"]["icao"]]["info"] = current_info
            current_data = planes[data["info"]["icao"]]["data"]  # Similar thing here, but using lists
            for datum in data["data"].keys():
                compiled = [data["data"][datum], message[1]]
                if datum not in current_data.keys():
                    current_data.update({datum: [compiled]})
                else:
                    current_data[datum].append(compiled)

        if "internal" not in planes[data["info"]["icao"]].keys():  # Update internal metrics
            planes[data["info"]["icao"]].update({"internal": {"last_update": message[1], "packets": 1,
                                                              "first_packet": message[1], "packet_type":
            {typecode_cat: 1}}})
        else:
            internal_data_storage = planes[data["info"]["icao"]]["internal"]
            internal_data_storage["last_update"] = message[1]
            internal_data_storage["packets"] += 1
            if typecode_cat in internal_data_storage["packet_type"].keys():
                internal_data_storage["packet_type"][typecode_cat] += 1
            else:
                internal_data_storage["packet_type"][typecode_cat] = 1
        messages.pop(0)
        if processed_messages % 10 == 0:
            print(processed_messages, planes)
    time.sleep(0.5)
