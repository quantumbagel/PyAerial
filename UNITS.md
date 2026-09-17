# Units

Rule `when` constraints and stored telemetry use the same units. The portal and Discord/Slack webhooks display altitude as metres plus feet, and speed as km/h plus knots.

| Metric | Stored unit | Source |
|--------|-------------|--------|
| Altitude | m | ADS-B feet × 0.3048 |
| Horizontal speed | km/h | ADS-B knots × 1.852, or geodesic distance / time |
| Vertical speed | m/s | ADS-B ft/min × 0.00508 |
| Heading / direction | ° clockwise from true north | ADS-B track, or great-circle bearing |
| Distance (`distance`) | km | geodesic to the **zone polygon** edge |
| Proximity (`proximity`) | m | same as `distance` × 1000 |
| ETA | s | time to the zone boundary along the projected path (`0` if already inside) |
| Latitude / longitude | degrees (WGS84) | ADS-B CPR |

Raw sensor frames on `/ws/raw` (not `/ws/live`) use:

| Field | Unit | Source |
|-------|------|--------|
| `timestamp` | unix seconds (float) | Engine receive time |
| `rssi` | dBFS | dump1090 Beast signal byte |
| `clock` | 12 MHz ticks (48-bit integer) | dump1090 Beast / `@` AVR; 1 tick = 1/12 000 000 s ≈ 83.3 ns. Free-running sample counter, not wall-clock time. |

A zone is a named polygon plus independent rules. A rule fires when every `when` constraint holds. Include `eta`, `distance`, or `proximity` to tie a rule to the zone.

Zone coordinates in config are `[latitude, longitude]`. Rule field details are in [CONFIGURATION.md](CONFIGURATION.md).
