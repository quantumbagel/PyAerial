"""Redis key names, TTLs, and the ``live:engine`` heartbeat payload.

These keys are internal (CONFIGURATION.md lists them); the engine's
:class:`~pyaerial.store.live.writer.LiveWriter` and the portal's
:class:`~pyaerial.store.live.reader.LiveReader` both use these names.
"""

from __future__ import annotations

import json
import time
from typing import Any

from pyaerial.constants import LIVE_ENGINE_TTL_SECONDS
from pyaerial.jsonutil import dumps as json_dumps

KEY_FLIGHTS = "live:flights"
KEY_FLIGHT = "live:flight:{flight_id}"
KEY_TELEMETRY = "live:telemetry:{flight_id}"
KEY_ALERTS = "live:alerts:{flight_id}"
KEY_ACTIVE_ALERTS = "live:active_alerts"
KEY_ALERT_EPISODES = "live:alert_episodes"
KEY_ENGINE = "live:engine"
RAW_CHANNEL = "live:raw"
# Safety net on per-flight keys (doc, telemetry, alerts), re-armed on every
# write. While an engine runs it pops finalized flights itself, so this only
# matters after a crash: the next engine archives the leftovers from these
# keys, so they must outlive any downtime we still want to recover from (a
# crash on Friday night found on Monday morning, plus slack). Past it the
# flight is lost from the archive rather than kept in Redis memory forever.
# Shared indexes (live:flights, alert hashes) cannot expire per member; the
# next engine drops entries whose keys are gone.
LIVE_FLIGHT_TTL_SECONDS = 7 * 86_400
# ``live:engine`` expires this long after its last write.
ENGINE_TTL_SECONDS = LIVE_ENGINE_TTL_SECONDS


def safe_json_loads(raw: Any) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def engine_heartbeat_payload(
    token: str, seen_at: float | None = None, extra: dict[str, Any] | None = None
) -> str:
    return json_dumps(
        {
            **(extra or {}),
            "seen_at": seen_at if seen_at is not None else time.time(),
            "token": token,
        }
    )


def parse_heartbeat(raw: Any) -> dict[str, Any] | None:
    """A stored heartbeat as a dict, or None when missing or not a JSON object."""
    if not raw:
        return None
    doc = safe_json_loads(raw)
    return doc if isinstance(doc, dict) else None


def heartbeat_seen_at(raw: str | bytes) -> float | None:
    """``seen_at`` of a stored heartbeat. Raises ValueError/TypeError if not JSON."""
    doc = json.loads(raw)
    seen = doc.get("seen_at") if isinstance(doc, dict) else None
    return float(seen) if isinstance(seen, (int, float)) else None


def heartbeat_owned_by(doc: Any, token: str) -> bool:
    return isinstance(doc, dict) and doc.get("token") == token


def heartbeat_is_fresh(doc: Any, now: float | None = None) -> bool:
    if not isinstance(doc, dict):
        return False
    seen = doc.get("seen_at")
    if not isinstance(seen, (int, float)):
        return False
    stamp = now if now is not None else time.time()
    return stamp - seen < ENGINE_TTL_SECONDS
