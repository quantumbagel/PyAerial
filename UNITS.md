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

**Raw sensor frames (Beast `/ws/beast` and TCP)**

| Field | Unit | Derivation |
|-------|------|------------|
| clock | 12 MHz ticks (48-bit) | dump1090 Beast / `@` AVR sample clock (1 tick ≈ 83.33 ns). AVR without `@` synthesizes from engine receive time. |
| signal | 0–255 | dump1090-fa `sqrt(signalLevel)*255`; JSON/UI RSSI is `20 * log10(byte / 255)` dBFS |

Geofence polygons are defined using `[latitude, longitude]` coordinates in decimal degrees, and rules referencing a zone must include at least one spatial parameter (`distance`, `proximity`, or `eta`) to calculate geometric boundaries as documented in [CONFIGURATION.md](CONFIGURATION.md).
