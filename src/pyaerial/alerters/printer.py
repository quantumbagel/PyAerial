"""Console logging alerter dispatching triggered rule records to stdout."""

from __future__ import annotations

from typing import ClassVar

from pyaerial.alerters import Alerter, register_alerter
from pyaerial.constants import (
    ALERT_CAT_ETA,
    ALERT_CAT_PAYLOAD,
    ALERT_CAT_REASON,
    ALERT_CAT_TYPE,
    ALERT_CAT_ZONE,
    STORE_CALLSIGN,
    STORE_ICAO,
)


@register_alerter("print")
class PrintAlerter(Alerter):
    known_options: ClassVar[frozenset[str]] = frozenset()

    def alert(self, meta: dict, payload: dict) -> None:
        reason = meta.get(ALERT_CAT_REASON)
        # Without the hook, activate and deactivate lines look identical.
        hook = reason.get("hook") if isinstance(reason, dict) else None
        record = {
            "hook": hook,
            "alert_id": meta.get("alert_id"),
            STORE_ICAO: meta[STORE_ICAO],
            STORE_CALLSIGN: meta.get(STORE_CALLSIGN),
            ALERT_CAT_TYPE: meta[ALERT_CAT_TYPE],
            ALERT_CAT_ZONE: meta[ALERT_CAT_ZONE],
            ALERT_CAT_ETA: meta[ALERT_CAT_ETA],
            ALERT_CAT_PAYLOAD: payload,
        }
        self.log.info("ALERT %s", record)
