# PyAerial



##  Project Concept

PyAerial is the successor to [the now archived airstrik.py](https://github.com/quantumbagel/airstrik.py). It has achieved full feature parity with its predecessor while also offering much more freedom for use cases.

PyAerial will scan for nearby planes using the ModeS/ADS-B protocol and provide an early-warning system for planes / helicopters that enter user-defined geofences, as well as programmable actions for the program to take (e.g. sending a POST request with data about the event or communicating with a Kafka server)

### Features

- Can handle ADS-B messages for altitude, position, airborne/landing velocities, callsign/geodesic, and more! See Junzi Sun's [book "The 1090 Megahertz Riddle" for more information.](https://mode-s.org/decode)
- [OpenSky Network](https://opensky-network.org/) integration for more information about plane ownership
- Smart ETA position calculations
- Alerts via Kafka that contain relevant information about the airplane (see example)
- MongoDB support / modular saving framework if other databases are needed
- Extremely versatile configuration with many different options for every possible use case

## Running

Install the package (from the repository root):

```bash
pip install -e .
```

Start the tracker (uses `./config.yaml`, or set `PYAERIAL_CONFIG` to another path):

```bash
python -m pyaerial
# or
pyaerial
# optional:
pyaerial --config /path/to/config.yaml
```

Configuration is validated on startup. The MongoDB stat viewer CLI:

```bash
pyaerial-statviewer
```

Optional: `PYAERIAL_OPENSKY_CSV` points to a CSV used for aircraft metadata (default: `./database.csv`).


## Formatting
### Configuration file example
```
general:
  mongodb: mongodb://localhost:27017
  backdate_packets: 10
  remember_planes: 30
  duplicate_packet_merging: 5
  status_message_top_planes: 5
  advanced_status: true
  hz: 2
  logs: info  # debug, info, warning, or error

home:
  latitude: 36.6810752
  longitude: -78.8758528

receivers:
  main:
    method: dump1090
    arguments:
      tcp_connection_ip: localhost
      tcp_connection_port: "30002"

components:
  easy:
    altitude:
      maximum: 10000
    eta:
      maximum: 120

zones:
  main:
    coordinates:
      [[35.753821, -78.909304],
      [35.755597, -78.904969],
      [35.756642, -78.898232]]
    levels:
      warn:
        category: really_high_priority
        requirements: easy
        seconds: 60

categories:
  really_high_priority:
    alert_method: print
    save:
      telemetry:
        default: all
        # optional per-field overrides, e.g. latitude: decimate(5)
      calculated:
        default: all

```

Requirement strings under `zones/.../levels/.../requirements` are boolean expressions over **component** names, for example `lenient and critical`, using only `and`, `or`, `not`, and parentheses.

Category `save` uses nested `telemetry` and `calculated` sections. Each section is a mapping that must include `default` (`all`, `none`, `decimate(N)`, `sdecimate(N,M)`) and may override individual fields (`latitude`, `altitude`, `horizontal_speed`, etc.).

Key names for the YAML file are defined in [pyaerial/constants.py](pyaerial/constants.py).



### Alert packet example
```
{'icao': 'AD61DE',
 'callsign': 'SWA1693',
 'type': 'warn', 
 'payload':
      {'altitude': 617.22,
       'latitude': 35.767181396484375,
       'longitude': -78.92131805419922},
 'zone': 'main',
 'eta': 52}  # This plane didn't have OpenSky integration, which is inside the 'opensky' key.
```

##  Configuration Options

| Configuration Option                           | What does it control?                                                                                                                                                                                                                                                                                                                                                 | constants.py variable               |
|------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------|
| `general`                                      | Contains options relative to the entire program's scope that didn't fit anywhere else.                                                                                                                                                                                                                                                                                | `CONFIG_GENERAL`                    |
| `general/mongodb`                              | The URI of the MongoDB instance to connect to if MongoDB is set as the method to save packets                                                                                                                                                                                                                                                                         | `CONFIG_GENERAL_MONGODB`            |
| `general/backdate_packets`                     | How many latitude/longitude packets back we look to perform a rough average when we calculate the heading. More = less variance, less = more variance.                                                                                                                                                                                                                | `CONFIG_GENERAL_BACKDATE`           |
| `general/remember_planes`                      | How many seconds since the last packet we should keep the plane in RAM before saving it to MongoDB                                                                                                                                                                                                                                                                    | `CONFIG_GENERAL_REMEMBER`           |
| `general/duplicate_packet_merging`           | Time window (seconds) for treating identical Mode S frames as duplicates across receivers.                                                                                                                                                                                                                | `CONFIG_GENERAL_MERGE_PACKETS`      |
| `receivers`                                  | Named receiver entries. Each has `method` (`dump1090` or `py1090`) and `arguments` (TCP host/port or RTL index).                                                                                                                                                                                        | `CONFIG_RECEIVERS`                  |
| `general/status_message_top_planes`            | How many of the "top planes" (most messages sent) to display in the status message sent every tick at the INFO logging level.                                                                                                                                                                                                                                         | `CONFIG_GENERAL_TOP_PLANES`         |
| `general/advanced_status`                      | Whether "advanced status" should be used. THis contains more data, with callsigns and packet type breakdowns. Example: `INFO:Main:Tracking 5 planes. Top 5: AB5DE1/WUP31 (358, {5: 50, 3: 51, 0: 253, 1: 4}), A3965C/FFT3373 (216, {0: 115, 5: 50, 3: 46, 1: 5}), A95A1C/AAL2349 (177, {0: 143, 3: 19, 5: 14, 1: 1}), A80D40/JBU2929 (21, {0: 13, 5: 3, 3: 4, 1: 1})` | `CONFIG_GENERAL_ADVANCED_STATUS`    |
| `general/hz`                                   | How many ticks per second to attempt. This is a maximum, not a minimum.                                                                                                                                                                                                                                                                                               | `CONFIG_GENERAL_HZ`                 |
| `home`                                         | Contains the position of the ADS-B tracker. This is used to calculate globally accurate positions from the ADS-B packets.                                                                                                                                                                                                                                             | `CONFIG_HOME`                       |
| `home/latitude`                                | The latitude of the ADS-B tracker                                                                                                                                                                                                                                                                                                                                     | `CONFIG_HOME_LATITUDE`              |
| `home/longitude`                               | The longitude of the ADS-B tracker                                                                                                                                                                                                                                                                                                                                    | `CONFIG_HOME_LONGITUDE`             |
| `zones`                                        | Contains information about the geofences and their different warning levels                                                                                                                                                                                                                                                                                           | `CONFIG_ZONES`                      |
| `zones/[zone]/coordinates`                     | A list of lists containing the decimal lat/long coordinates that compose the geofence.                                                                                                                                                                                                                                                                                | `CONFIG_ZONES_COORDINATES`          |
| `zones/[zone]/levels`                          | Contains information about the levels of triggers the geofence has.                                                                                                                                                                                                                                                                                                   | `CONFIG_ZONES_LEVELS`               |
| `zones/[zone]/levels/[level]/category`         | The category (information about how to save and alert) that this level of the geofence is tied to                                                                                                                                                                                                                                                                     | `CONFIG_ZONES_LEVELS_CATEGORY`      |
| `zones/[zone]/levels/[level]/seconds`          | Number of simulated seconds the requirement expression must hold to qualify for persistence for that level.                                                                                                                                                                                                                                                             | `CONFIG_ZONES_LEVELS_SECONDS`       |
| `components`                                 | Named rule sets (e.g. altitude caps) referenced from zone level `requirements` expressions.                                                                                                                                                                                                                                                                            | `CONFIG_COMPONENTS`                 |
| `categories`                                 | Named alert/save profiles referenced by zone levels (`category` field).                                                                                                                                                                                                                                                                                               | `CONFIG_CATEGORIES`                 |
| `categories/[category]/alert_method`           | Which method to use when alerting. Current options: `print`, `kafka`                                                                                                                                                                                                                                                                                                  | `CONFIG_CAT_METHOD`                 |
| `categories/[category]/arguments`               | If applicable, put arguments for the method in here. The only option is `server` for the `kafka` method currently.                                                                                                                                                                                                                                                    | `CONFIG_CAT_ALERT_ARGUMENTS`        |
| `categories/[category]/save/telemetry`       | Save filters for raw receiver-derived fields. Must include `default`; optional per-field keys override `default` for that quantity.                                                                                                                                                                                                                                      | `CONFIG_CAT_SAVE` + `telemetry`     |
| `categories/[category]/save/calculated`      | Save filters for derived fields (speed, heading, …). Same structure as `telemetry`.                                                                                                                                                                                                                                                                                  | `CONFIG_CAT_SAVE` + `calculated`    |


## Dependencies

```
shapely  # Other geographic math.
geopy  # Some geographic math, mostly distance.
pymodes  # some ADS-B decoding hex math
requests  # hexdb.io's ICAO callsign API
pyrtlsdr  # Pure-python ADS-B decoder
kafka-python  # Using the Kafka alert method
pymongo  # Interfacing with MongoDB
ruamel.yaml  # For reading the configuration file
```

`dump1090-fa` is required for the `dump1090` packet method to function. The command `dump1090 --net --raw` should work out-of-the-box. You can also broadcast raw ADSB messages over TCP port `30002` and the interface will also work.

## TODOS

- [ ] KML import for geofence coordinates
- [ ] Deeper stat viewer queries / `history` command
- [ ] Optional live OpenSky API integration (CSV path is supported via `PYAERIAL_OPENSKY_CSV`)
- [ ] Further ETA / ray intersection review and regression cases

Feel free to report any bugs or issues you find. Happy tracking!
