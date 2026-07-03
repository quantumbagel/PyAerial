"""Alerter that logs alerts (the default, dependency-free option)."""
from __future__ import annotations

from pyaerial.alerters import Alerter, register_alerter
from pyaerial.constants import (
    ALERT_CAT_ETA,
    ALERT_CAT_PAYLOAD,
    ALERT_CAT_TYPE,
    ALERT_CAT_ZONE,
    STORE_CALLSIGN,
    STORE_ICAO,
)


@register_alerter("print")
class PrintAlerter(Alerter):
    def alert(self, meta: dict, payload: dict) -> None:
        record = {
            STORE_ICAO: meta[STORE_ICAO],
            STORE_CALLSIGN: meta.get(STORE_CALLSIGN),
            ALERT_CAT_TYPE: meta[ALERT_CAT_TYPE],
            ALERT_CAT_ZONE: meta[ALERT_CAT_ZONE],
            ALERT_CAT_ETA: meta[ALERT_CAT_ETA],
            ALERT_CAT_PAYLOAD: payload,
        }
        self.log.info("ALERT %s", record)
