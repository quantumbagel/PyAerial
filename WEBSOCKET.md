# WebSocket API

`pyaerial web` serves live state, history queries and raw Mode S frames.

**Endpoints**

| Endpoint | Format | Purpose |
|----------|--------|---------|
| `ws://<host>:<port>/ws/live` (alias `/ws`) | JSON | Push streams and RPC (`hello.protocol: "pyaerial.live"`, version 1) |
| `ws://<host>:<port>/ws/raw` | JSON | Corrected Mode S frames (portal Raw tab) |
| `ws://<host>:<port>/ws/beast` | Binary | The same frames as dump1090 Beast |
| `tcp://<beast_host>:<beast_port>` | Binary | Same bytes as `/ws/beast`, default port 30105 |
| `GET /api` | JSON | Discovery document, including the active `limits` |
| `GET /health` | JSON | Liveness: `{"status": "ok"}` |
| `GET /ready` | JSON | `status`, `redis`, `history`, `engine`; HTTP 503 while Redis is unreachable |
| `GET /metrics` | Prometheus text 0.0.4 | Engine and portal metrics (below); off with `web.metrics: false` |
| `/docs`, `/redoc`, `/openapi.json` | HTML / JSON | Only with `web.enable_docs: true` |

`/ws/raw`, `/ws/beast` and Beast TCP send no `hello`, accept no RPC and send no `ping`.

**Metrics**

`/metrics` costs one Redis GET (the `live:engine` heartbeat) plus the `/ready` checks. Point Prometheus at it directly (e.g. `http://127.0.0.1:10090/metrics`).

- Portal: `pyaerial_build_info{version}` (always 1), `pyaerial_redis_up`, `pyaerial_archive_up` (as in `/ready`), `pyaerial_websocket_connections{endpoint}` (`/ws/live`, including `/ws`; `/ws/raw`; `/ws/beast`), `pyaerial_beast_tcp_clients`, `pyaerial_websocket_queued_bytes` (`/ws/live` responses and telemetry waiting to be sent), `pyaerial_websocket_rate_limited_total` (requests answered `rate_limited`), `pyaerial_websocket_clients_dropped_total{reason}` (closes with `1013`: `client_too_slow` and `server_budget` on `/ws/live`, `feed_behind` on `/ws/raw` and `/ws/beast`) and `pyaerial_beast_tcp_clients_dropped_total{reason}` (`feed_behind`, `not_reading`).
- Engine, from the heartbeat: `pyaerial_engine_up` (1 while the heartbeat is fresh). Only while it is 1: `pyaerial_engine_start_time_seconds`, `pyaerial_engine_planes_tracked`, `pyaerial_engine_history_up`, `pyaerial_engine_history_pending`, and the counters `pyaerial_engine_frames_received_total`, `_frames_dropped_total`, `_messages_processed_total`, `_pending_evicted_total`, `_receiver_restarts_total{receiver}` and `_stage_errors_total{stage}`. Counters restart from 0 with the engine.

Labels carry receiver names, never hosts, ports or credentials. `web.allowed_hosts` applies as on every route; a request with a browser `Origin` must pass the WebSocket origin rules, else HTTP 403.

**Origins**

Browsers must present an allowed `Origin`; others are closed with `1008` (`origin not allowed`). Native clients that omit `Origin` are accepted. Allowed:

- Same origin (`Origin` host and port equal the `Host` header), when the portal is reached by IP address, `localhost`, or a hostname listed in `web.allowed_hosts`.
- Exact matches in `web.origins` (`https://portal.example` does not match `http://portal.example` or `https://portal.example:8443`); `["*"]` allows any.
- Any `localhost`, `127.0.0.1` or `[::1]` origin with `web.allow_localhost_origins: true`.

**Limits**

| Limit | Default (config key) | When exceeded |
|-------|----------------------|---------------|
| WebSocket connections, all endpoints | 256 (`web.ws_max_connections`) | Close `1013` (`server connection limit reached`) |
| Connections per client IP | 32 (`web.ws_max_connections_per_ip`) | Close `1013` (`per-client connection limit reached`) |
| In-flight RPC requests per connection | 2 (`web.ws_max_inflight`) | The server stops reading the socket until a response has been sent |
| RPC rate per connection | 50/s, burst 100 (`web.ws_rate_limit`, `web.ws_rate_burst`) | Requests are delayed; one that would wait over 1 s gets `code: "rate_limited"` |
| RPC rate per client IP | 200/s, burst 400 (`web.ws_rate_limit_per_ip`, `web.ws_rate_burst_per_ip`) | Same |
| Inbound frame size | 64 KiB (fixed) | Close `1009` |
| Outbound queue per client | 64 messages / 8 MB (fixed) | Stream messages are skipped or replaced by newer snapshots. A client whose unread RPC responses fill it is closed with `1013` (`client too slow`) |
| Outbound queues, all clients | 256 MB (fixed) | The client with the largest backlog is closed with `1013` (`server send budget exceeded`) |
| Slow `/ws/raw` or `/ws/beast` reader | 10 s behind (fixed) | Close `1013` (`client too slow`) |
| Beast TCP feeders | 16 total, 4 per IP (`web.beast_max_clients`, `web.beast_max_clients_per_ip`) | Extra connections are closed. A feeder that stops reading or falls 10 s behind is dropped |

Behind a reverse proxy, list it in `web.trusted_proxies`, or all clients share the proxy's IP for the per-IP limits.

**Live feed (`/ws/live`)**

On connect the server sends `hello`, then snapshots of the subscribed `flights`, `alerts` and `stats` streams, then pushes changes. It sends `{"type": "ping", "timestamp": ...}` every 15 s.

```json
{"type": "hello", "protocol": "pyaerial.live", "version": 1,
 "streams": ["flights", "alerts", "telemetry", "stats"],
 "actions": ["subscribe", "fetchFlights", "fetchFlight", "fetchTelemetry", "fetchAlerts", "fetchStats", "fetchZones", "fetchConfig"]}
```

The exact message shapes (which keys are always sent, which may be `null`, which are left out) are the pydantic models in `src/pyaerial/api/models.py`, published as JSON Schema in [`web/src/api/schema.json`](web/src/api/schema.json).

Clients must ignore unknown fields and message types; `version` changes only when something is removed or retyped (see Compatibility in the [README](README.md)).

All four streams are on by default; choose with `/ws/live?streams=flights,alerts` or `subscribe`. `ping` is not a stream. While Redis is unreachable, `flights` and `alerts` are not sent and `stats.redis` is `false`.

| Message | Payload | Sent |
|---------|---------|------|
| `flights` | `flights`: every live flight summary | When positions, metadata, alerts or integrity change |
| `alerts` | `alerts`: every active episode plus the newest inactive ones, `max(50, active count)` in total, newest first | On change; `fetchAlerts` returns the full list |
| `telemetry` | `timestamp`, `telemetry`: points newer than the last one this client received for each flight | Only points that arrive after connecting (or subscribing); use `fetchTelemetry` for the backlog |
| `stats` | `stats`: `live_flights`, `active_alerts`, `retained_flights`, `historical_alerts`, `redis`, `history`, `engine_seen_at`, `engine_live` | On change |

Flight summary (live and history views share these fields):

```json
{
  "flight_id": "a1b2c3-1721832000", "icao": "a1b2c3", "callsign": "AAL123",
  "model": "Boeing 737-800", "owner": "American Airlines", "country": "United States",
  "aircraft_type": "L2J", "registration": "N123AA",
  "start_time": 1721832000.0, "end_time": 1721832015.2, "timestamp": 1721832015.2,
  "latitude": 35.7288, "longitude": -78.6954, "altitude": 1250.0, "speed": 420.5, "heading": 185.0,
  "is_live": true, "status": "live", "retained": false,
  "integrity": {"score": 1.0, "status": "ok",
                "checks": {"radio_horizon": {"status": "pass", "detail": "main: 42 km, radio horizon 441 km", "value": 0.1}}},
  "active_alerts": [{"alert_id": "a1b2c3-1721832000:airport_approach:low_altitude_warning",
                     "zone": "airport_approach", "rule": "low_altitude_warning", "activated_at": 1721832010.0, "eta": 45.0}],
  "alert_stats": {"episode_count": 1, "total_seconds": 5, "active_count": 1}
}
```

- Units follow [CONFIGURATION.md](CONFIGURATION.md): m, km/h, degrees, unix seconds.
- Unknown metadata is `null`; placeholders such as `"UNKNOWN"` or `""` are never sent.
- `integrity` is `null` until the engine scores the aircraft. Otherwise it has `status`, `score` (`null` while `unknown`), `checks`, and when present `emitter` and `rebroadcast`. See "Integrity output" in [CONFIGURATION.md](CONFIGURATION.md).
- `active_alerts[].alert_id` is never empty (`<flight_id>:<zone>:<rule>` when the store has no id).

Alert episodes carry `alert_id`, `flight_id`, `icao`, `callsign`, `zone`, `rule`, `active`, `activated_at`, `deactivated_at`, `ended_by`, `eta`, `altitude`, `latitude` and `longitude`. `ended_by` (`cleared`, `lost`, `shutdown`, `restart`; see "Alerters" in [CONFIGURATION.md](CONFIGURATION.md)) is `null` while active or unknown.

Telemetry points carry `flight_id`, `icao`, `latitude`, `longitude`, `altitude` (barometric), `speed`, `heading` and `timestamp`. `gnss_height` is present only for GNSS-height positions. When provenance is known they also carry `receivers` (strongest copy first) and the verdict in effect when the point arrived: `integrity_score` (`null` while `unknown`), `integrity_status` and `integrity_flags`.

`stats.engine_live` is computed on the server (heartbeat within 10 s); prefer it to comparing `engine_seen_at` with the browser clock.

**RPC**

Send requests on `/ws/live`:

```json
{"type": "request", "id": "req-42", "action": "fetchFlight", "params": {"flightId": "a1b2c3-1721832000", "view": "live"}}
```

```json
{"type": "response", "id": "req-42", "success": true, "data": {"flight_id": "a1b2c3-1721832000", "photo_url": "https://images.planespotters.net/photo/..."}}
{"type": "response", "id": "req-42", "success": false, "error": "not found", "code": "not_found"}
```

`id` is echoed when it is a string of at most 128 characters or an integer; otherwise it is `null`. `error` is for humans; branch on `code`:

| `code` | Meaning |
|--------|---------|
| `bad_request` | Malformed JSON, binary frame, unknown action, missing `flightId`, or a non-numeric / non-finite `since` / `until` |
| `not_found` | `fetchFlight` found no such flight |
| `rate_limited` | Over the rate limit by more than the server will delay; retry later |
| `history_unavailable` | The SQLite archive is configured but cannot be read |
| `live_unavailable` | Redis is unreachable |
| `internal_error` | Unexpected server error (logged) |

`view` is `"live"` (default) or `"history"`. Times are unix seconds.

| Action | Parameters | Returns |
|--------|------------|---------|
| `subscribe` | `streams`: names; omit or `[]` for all four | `{"streams": [...]}`. Replaces the current set. New `flights`, `alerts` and `stats` streams get a snapshot at once; new `telemetry` starts from now. Only unknown names: `bad_request` |
| `fetchFlights` | `view`; history only: `skip` (0-10000), `limit` (1-200, default 50), `q` (ICAO, callsign or flight ID substring), `since` / `until` (on end time) | Flight summaries. The live view returns all live flights |
| `fetchFlight` | `flightId` (required), `view` | Flight detail with airframe metadata and photo links (`photo_url`, `photo_photographer`, `photo_link`) |
| `fetchTelemetry` | `flightId` (required), `view`, `since` (default 0), `limit` (1-20000, default 5000) | Points in time order, with `truncated` beside `data`. Without `since`, the newest points; with it, the oldest after it, so page forward from the last timestamp. Live reads the Redis trail and is empty while the engine heartbeat is stale |
| `fetchAlerts` | `view`, `flightId`, `rule`, `active_only`, `since`, `skip` (0-10000), `limit` (0-200); history also `until` and `q` (zone, rule, callsign or ICAO) | Alert episodes. `limit: 0` means all (live) or 200 (history). Live defaults to active episodes unless `flightId` is given |
| `fetchStats` | | Same object as the `stats` stream. Live counts are 0 and `engine_live` is `false` while the heartbeat is stale; `engine_seen_at` is `null` if none was ever written |
| `fetchZones` | | Station `home`, zone polygons and `alert_colors` |
| `fetchConfig` | | `home`, `remember_planes`, `receivers` (`name`, `type`, and when known `format` and fixed `position`; no hosts or ports), `beast_websocket`, and `beast_host` / `beast_port` when Beast TCP is on |

**Raw frames (`/ws/raw`)**

```json
{"type": "raw", "frames": [{"hex": "8d406b902015a678d4d220aa4bda", "timestamp": 1730000000.125, "receiver": "strong",
  "receivers": ["strong", "weak"], "rssi": -8.0, "clock": 12, "df": 17, "icao": "406b90"}]}
```

`timestamp` is engine receive time. `receiver` is the strongest copy; `receivers` lists every receiver that heard the frame, strongest first. `rssi` is dBFS.

**Beast feed (`/ws/beast` and TCP)**

PyAerial does not forward dump1090's socket. It merges the frames of all receivers (an exact duplicate keeps the stronger RSSI; near-identical copies within 100 ms keep a CRC-clean one), drops DF17/18 frames that fail CRC, and skips frames already sent within `tracking.duplicate_packet_merging` seconds. The rest are re-encoded as dump1090 Beast:

```
<0x1a> <type> <6-byte 12 MHz clock> <1-byte signal> <Mode S payload>
```

- `type`: `0x31` (Mode A/C, 2 bytes), `0x32` (Mode S short, 7 bytes) or `0x33` (Mode S long, 14 bytes). Any `0x1a` byte after the type is doubled.
- Clock: derived from engine receive time, `int(timestamp * 12e6) mod 2^48`, so all receivers share one time base. Hardware dump1090 clocks are not mixed in.
- Signal: dump1090-fa's `sqrt(signalLevel) * 255` byte. dBFS = `20 * log10(byte / 255)`.

Beast TCP (`web.beast_host` / `web.beast_port` in [CONFIGURATION.md](CONFIGURATION.md)) is what OpenSky Network's feeder expects:

```yaml
opensky:
  image: ghcr.io/sdr-enthusiasts/docker-opensky-network:latest
  environment:
    - BEASTHOST=pyaerial_web   # or the host running `pyaerial web`
    - BEASTPORT=30105
    - LAT=35.727488
    - LONG=-78.695942
    - ALT=120
    - OPENSKY_USERNAME=youruser
```

**Python client**

```python
import asyncio, json
import websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:10090/ws/live?streams=flights,alerts") as ws:
        assert json.loads(await ws.recv())["type"] == "hello"
        await ws.send(json.dumps({"type": "request", "id": "hist", "action": "fetchFlights",
                                  "params": {"view": "history", "limit": 10}}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "flights":
                for f in msg["flights"]:
                    print(f["icao"], f["callsign"], f["latitude"], f["longitude"], f["altitude"])
            elif msg["type"] == "alerts":
                for a in msg["alerts"]:
                    if a["active"]:
                        print("ALERT", a["zone"], a["rule"], a["icao"], a["eta"])
            elif msg["type"] == "response" and msg["id"] == "hist":
                print("history:", [f["flight_id"] for f in msg.get("data") or []])

asyncio.run(main())
```
