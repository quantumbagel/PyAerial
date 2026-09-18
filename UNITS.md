# Units

Telemetry buffers, SQLite records, and rule evaluations operate strictly in SI and WGS84 base units, while outbound webhooks and the web portal project auxiliary imperial values (feet, knots) alongside metric state.

**Stored telemetry and rule metrics**

| Metric | Stored unit | Derivation |
|--------|-------------|------------|
| Altitude | m | ADS-B feet × 0.3048 |
| Horizontal speed | km/h | ADS-B knots × 1.852, or geodesic distance over time |
| Vertical speed | m/s | ADS-B ft/min × 0.00508 |
| Heading / direction | ° clockwise from true north | ADS-B track, or forward great-circle azimuth |
| Distance (`distance`) | km | Geodesic distance to nearest polygon perimeter edge |
| Proximity (`proximity`) | m | `distance` × 1000 |
| ETA | s | Kinematic travel time to polygon boundary along projected track (`0` if inside) |
| Position | degrees (WGS84) | Compact Position Reporting (CPR) latitude and longitude |

**Raw sensor frames (`/ws/raw`)**

| Field | Unit | Derivation |
|-------|------|------------|
| `timestamp` | unix epoch seconds (float) | Engine reception timestamp |
| `rssi` | dBFS | dump1090 Beast signal level |
| `clock` | 12 MHz ticks (48-bit integer) | dump1090 Beast / `@` AVR sample clock (1 tick ≈ 83.33 ns; free-running hardware counter, not wall clock) |

Geofence polygons are defined using `[latitude, longitude]` coordinates in decimal degrees, and rules referencing a zone must include at least one spatial parameter (`distance`, `proximity`, or `eta`) to calculate geometric boundaries as documented in [CONFIGURATION.md](CONFIGURATION.md).
