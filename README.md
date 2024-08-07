# PyAerial

_scanning software for ADS-B / ModeS
a.k.a. airstrik 2.0_


## General Project Concept

PyAerial is the successor to airstrik.py.

PyAerial will scan for nearby planes using the ModeS/ADS-B protocol and provide an early-warning system for planes / helicopters that enter user-defined geofences, as well as programmable actions for the program to take (e.g. sending a POST request with data about the event or communicating with a Kafka server)


## Formatting
### Configuration file example
```
general:
  mongodb: mongodb://localhost:27017
  kafka: "localhost:12345"
  backdate_packets: 10
  remember_planes: 30
  point_accuracy_threshold: 10  # degrees
  packet_method: dump1090
  status_message_top_planes: 5
  hz: 2

home:
  latitude: 36.6810752
  longitude: -78.8758528


zones:
  main:  # smaller area randomly picked out for testing
    coordinates:
      [[35.753821, -78.909304],
      [35.755597, -78.904969],
      [35.756642, -78.898232],
      [35.755214, -78.892738],
      [35.753333, -78.888490],
      [35.749293, -78.889606],
      [35.747343, -78.891494],
      [35.746507, -78.895742],
      [35.747482, -78.900806],
      [35.748910, -78.906085],
      [35.751348, -78.910205]]
    levels:
      warn:
        category: really_high_priority
        time: 45
        send: true
      alert:
        category: really_high_priority
        time: 0
        send: true

  alternate: # Most of raleigh area
    coordinates:
      [[36.279595, -79.349321],
      [35.943933, -79.534100],
      [35.560548, -79.501141],
      [35.058494, -79.138592],
      [35.049501, -78.776043],
      [35.184300, -78.248700],
      [35.435327, -78.017987],
      [35.801494, -77.963055],
      [36.112746, -77.974041],
      [36.254624, -78.226727],
      [36.334318, -78.666180],
      [36.325467, -78.929852],
      [36.343168, -79.259442]]
    levels:
      warn:
        category: warn
        time: 120
      alert:
        category: alert
        time: 0

categories:
  really_high_priority:
    priority: 1000000
    method: print
    save:
      telemetry_method: all
      calculated_method: all
  warn:
    priority: 10
    method: kafka
    arguments:
      server: "localhost:60403"
    save:
      telemetry_method: all
      calculated_method: all
  alert:
    priority: -1
    method: print
    save:
      telemetry_method: all
      calculated_method: all
```

All names for anything can be customized in the `constants.py` file.



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
 'eta': 52}
```

##  Configuration Options

| Configuration Option                           | What does it control?                                                                                                                                                            | constants.py variable               |
|------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------|
| `general`                                      | Contains options relative to the entire program's scope that didn't fit anywhere else.                                                                                           | `CONFIG_GENERAL`                    |
| `general/mongodb`                              | The URI of the MongoDB instance to connect to if MongoDB is set as the method to save packets                                                                                    | `CONFIG_GENERAL_MONGODB`            |
| `general/backdate_packets`                     | How many latitude/longitude packets back we look to perform a rough average when we calculate the heading. More = less variance, less = more variance.                           | `CONFIG_GENERAL_BACKDATE`           |
| `general/remember_planes`                      | How many seconds since the last packet we should keep the plane in RAM before saving it to MongoDB                                                                               | `CONFIG_GENERAL_REMEMBER`           |
| `general/point_accuracy_threshold`             | The degree accuracy used for determining if we should "chase" geofences. This is just an optimization.                                                                           | `CONFIG_GENERAL_PAT`                |
| `general/packet_method`                        | How the program should gather packets. Options: `dump1090` or `python`. Dump1090 is significantly better, but requires `dump1090 --raw --net` to be running in another terminal. | `CONFIG_GENERAL_PACKET_METHOD`      |
| `general/status_message_top_planes`            | How many of the "top planes" (most messages sent) to display in the status message sent every tick at the INFO logging level.                                                    | `CONFIG_GENERAL_TOP_PLANES`         |
| `general/hz`                                   | How many ticks per second to attempt. This is a maximum, not a minimum.                                                                                                          | `CONFIG_GENERAL_HZ`                 |
| `home`                                         | Contains the position of the ADS-B tracker. This is used to calculate globally accurate positions from the ADS-B packets.                                                        | `CONFIG_HOME`                       |
| `home/latitude`                                | The latitude of the ADS-B tracker                                                                                                                                                | `CONFIG_HOME_LATITUDE`              |
| `home/longitude`                               | The longitude of the ADS-B tracker                                                                                                                                               | `CONFIG_HOME_LONGITUDE`             |
| `zones`                                        | Contains information about the geofences and their different warning levels                                                                                                      | `CONFIG_ZONES`                      |
| `zones/[zone]/coordinates`                     | A list of lists containing the decimal lat/long coordinates that compose the geofence.                                                                                           | `CONFIG_ZONES_COORDINATES`          |
| `zones/[zone]/levels`                          | Contains information about the levels of triggers the geofence has.                                                                                                              | `CONFIG_ZONES_LEVELS`               |
| `zones/[zone]/levels/[level]/category`         | The category (information about how to save and alert) that this level of the geofence is tied to                                                                                | `CONFIG_ZONES_LEVELS_CATEGORY`      |
| `zones/[zone]/levels/[level]/time`             | The maximum ETA the plane must have relative to the geofence to trigger this level.                                                                                              | `CONFIG_ZONES_LEVELS_TIME`          |
| `categories`                                   | Stores the categories (information for how alerts and saving works). Categories will only be used if they are put in at least one geofence                                       | `CONFIG_CATEGORIES`                 |
| `categories/[category]/method`                 | Which method to use when alerting. Current options: `print`, `kafka`                                                                                                             | `CONFIG_CAT_METHOD`                 |
| `categories[category]/arguments`               | If applicable, put arguments for the method in here. The only option is `server` for the `kafka` method currently.                                                               | `CONFIG_CAT_ALERT_ARGUMENTS`        |
| `categories/[category]/save`                   | Filters for saving to MongoDB. Existing methods: `all`, `none`, `decimate(X)`, `sdecimate(X,Y)`                                                                                  | `CONFIG_CAT_SAVE`                   |
| `categories/[category]/save/telemetry_method`  | What method to use for the telemetry data (stuff received by the ADS-B receiver with no inference).                                                                              | `CONFIG_CAT_SAVE_TELEMETRY_METHOD`  |
| `categories/[category]/save/calculated_method` | What method to use for the calculated data (stuff we inferred from the ADS-B information, including corrected versions of telemetry data we already receive).                    | `CONFIG_CAT_SAVE_CALCULATED_METHOD` |


## Dependencies

```
shapely
geopy
pymodes
requests
pyrtlsdr
kafka-python
pymongo
ruamel.yaml
```

`dump1090` is required for the `dump1090` packet method to function. You can also broadcast raw ADSB messages over TCP port `30002` and the interface will also work.

Feel free to report any bugs or issues you find. Happy tracking!