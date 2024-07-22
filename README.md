# PyAerial

_scanning software for ADS-B / ModeS
a.k.a. airstrik 2.0_


## General Project Concept

PyAerial is intended to be a successor to airstrik.py.


PyAerial will scan for nearby planes using the ModeS/ADS-B protocol and provide an early-warning system for planes / helicopters that enter user-defined geofences, as well as programmable actions for the program to take (e.g. sending a POST request with data about the event or communicating with a Kafka server)

This is intended to be a document describing how PyAerial should function (so there's no question later along). New concepts will be added to this document, as well as a brief description of why the feature was added.


PyAerial addresses many of airstrik.py's problems like:


## Problems (with airstrik.py) that are resolved
### File IO latency

Because airstrik.py relied on dump1090's JSON dump feature, it had to grapple with slower speeds and inconsistent tick timing brought on by large amounts of file IO. PyAerial will not have this problem because it will handle the decoding process by itself.


### Drop support for dump978
PyAerial will drop support for dump978, as its quantity of data isn't sufficient. Every plane that shows on 978mhz is on 1090mhz, and it just is not worth maintaining support for UAT 978mhz. This will allow 1090mhz specific handling of packets.

### ICAO decoding for more planes
PyAerial will calculate the ICAO decoding (instead of using a dictionary) for American and Canadian planes, the vast majority of planes in NC. If we see a plane that isn't recognized, PyAerial will attempt to classify it according to this list: https://www.aerotransport.org/html/ICAO_hex_decode.html. Nationality will be stored in MongoDB.

## Improvements from airstrik.py
### Better stats

PyAerial will store daily stat metrics including
- total unique planes
- total (concluded) plane trips
- Average trip length

This may even be in a separate MongoDB database. I will flesh this out a little bit later when I have ideas on stats to store.

### Better Metrics

PyAerial will store additional plane-specific metrics like

- Nationality for each stored plane that we've seen
- Plane call sign
- times we've seen the plane
- total time we've seen the plane
- trips we've seen the plane
- Total packets received by the plane

Also, PyAerial will store trip-specific metrics like

- Length of trip
- Packets received over trip
- Log of zone entries/leaves with timestamp
- Formatting of this:
   - time: 192832989 (unix)
   - entries:
   - event: "entered", zone: "circle", priority: -10


### Better zones

Each zone can now be defined as a geofence (list of points) that will be checked for ETA as normal: (thanks turfpy)
https://stackoverflow.com/questions/43892459/check-if-geo-point-is-inside-or-outside-of-polygon

Zones can also be defined as a simple circle. I may also add other methods in the future.


## Formatting
### Configuration file example
```
home: # Home lat/long point
lat: 35.7270309
lon: -78.695587
remember: 60
mongo_address: "127.0.0.1:27017"

zones:
	area:
		type: geofence
		coordinates:
			(1, 1)
			(2, 2)
			(3,3)
		altitude: 3000
		classification: normal
		
	circle:
		type: circle
		radius: 100
		altitude: 2000
		classification: serious

classifications:
	normal:
		priority: 10
		warn_method: get_request
		warn_for_incoming: false
		predict: 30  # Only "try" for 30 seconds to intersect
		save:
			decimate: 3
			mark_important: false
		ip: google.com
	serious:
		priority: -100
		warn_method: kafka_server
		warn_for_incoming: true
		predict: 60
		ip: examplekafka.com/connect
		username: root
		password: password
		save:
			decimate: 0
			mark_important: true
```


### Alert packet
```
{"type": "alert".
 STORE_ICAO: "09fs0df",
 "tag": "3c3c3c",   # No tag = incalculable / not provided by plane
 "reason": {"zone": "area", "classification": "normal"},
"latitude": 3249.92034,
"longitude": 43.3948,
"altitude": 3000}
```

### Warning packet
```
{"type": "warning".
 STORE_ICAO: "09fs0df",
 "tag": "3c3c3c",   # No tag = incalculable / not provided by plane
 "reason": {"zone": "circle", "classification": "serious"},
"latitude": 3249.92034,
"longitude": 43.3948,
"altitude": 3000,
"eta": 53}
```


## Dependencies

pyturf - Geofence calculations
ruamel.yaml - YAML configuration file
pymodes - Packet decoding
geopy - some distance calculations
pymongo - Database operations
kafka-python - Send alert to kafka server when intrusion
requests - Handle POST request with headers for "intruders"






## Project Checklist

These are just things that have to happen. I don't know what order to do them in (fully)

1. **Set up PyModeS / Test PyModeS**
    - Run tests with local 1090mhz receiver / compare with dump1090
    - Get mainloop that just prints out packets as they are received
2. **Mainloop (basic)**
    - Get each packet and save it to MongoDB
    - Redundancy calculation
3. **Prediction**
    - Calculate
        - heading
        - speed
        - (rip it out of airstrik.py)
    - Predict ahead for the maximum amount of time allotted by any filter
        - Maybe smart optimizations (could be a cool problem)
    - Determine which zone/classification to use
4. **Handling**
    - Read data from classification
    - Send requests:
        - implement kafka sending
        - Implement get/post requests
5. **Validation**
    - ensure that all configuration data is valid
    - yea that's it
6. **Mainloop (saving data)**
    - Store data on each plane-trip with:
        - Zones entered
        - Zones warned
        - Travel time
    - When we lose sight of planes:
        - Get classification of plane-trip
           - Determine data compression
            - decimate: remove nth unique packet, blindly
            - smart_decimate: store (up to) one packet per minute, or settable amount
            - all: ALL THE DATA
            - info: just info like travel time, travel distance, hex code, etc (included in all others btw)
        - Compress data and save
