# PyAerial Units

Every calculated or received metric and its corresponding unit.

| Metric | Stored unit | Source |
|---|---|---|
| Altitude | metres (`m`) | ADS-B feet × 0.3048 |
| Horizontal speed | kilometres per hour (`km/h`) | ADS-B knots × 1.852, or geodesic distance / time |
| Vertical speed | metres per second (`m/s`) | ADS-B ft/min × 0.00508 |
| Heading / direction | degrees clockwise from true north (`°`) | ADS-B track, or great-circle bearing |
| Distance (rule field `distance`) | kilometres (`km`) | geodesic to the **zone polygon** edge |
| Proximity (rule field `proximity`) | metres (`m`) | same as `distance`, × 1000 |
| ETA | seconds (`s`) | time to enter the zone along the projected path |
| Latitude / longitude | degrees (WGS84) | ADS-B CPR |

Zone coordinates in config are `[latitude, longitude]`.
MongoDB GeoJSON positions are `[longitude, latitude]`.

The web portal and Discord/Slack webhooks display altitude as m + ft and speed as km/h + kt.
