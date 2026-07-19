import logging
import requests
from pyaerial.alerters import Alerter, register_alerter

log = logging.getLogger("pyaerial.alerter.webhook")

@register_alerter("webhook")
class WebhookAlerter(Alerter):
    """Sends JSON alerts to a configured HTTP webhook endpoint."""

    def configure(self, arguments: dict) -> None:
        self.url = arguments.get("url")
        if not self.url:
            raise ValueError("webhook URL must be provided in alerter options")
        self.headers = arguments.get("headers", {})
        self.method = arguments.get("method", "POST")
        self.payload_format = arguments.get("format", "json")  # json, discord, slack

    def alert(self, meta: dict, payload: dict) -> None:
        alert_data = {**meta, "telemetry": payload}
        
        # Format payload for common systems
        if self.payload_format == "discord":
            level_emoji = "🚨" if meta.get("type") == "alert" else "⚠️"
            body = {
                "embeds": [{
                    "title": f"{level_emoji} PyAerial: {meta.get('type', '').upper()} Warning",
                    "description": f"Aircraft **{meta.get('callsign') or 'Unknown'}** ({meta.get('icao', '').upper()}) has met alert criteria in zone **{meta.get('zone')}**.",
                    "fields": [
                        {"name": "Altitude", "value": f"{payload.get('altitude')} ft" if payload.get('altitude') is not None else "N/A", "inline": True},
                        {"name": "Estimated Arrival (ETA)", "value": f"{int(meta.get('eta'))}s" if meta.get('eta') is not None else "Unknown", "inline": True},
                    ],
                    "color": 15570228 if meta.get("type") == "alert" else 16752651
                }]
            }
        elif self.payload_format == "slack":
            body = {
                "text": f"*{meta.get('type', '').upper()}*: Aircraft `{meta.get('callsign') or 'Unknown'}` ({meta.get('icao', '').upper()}) met conditions in `{meta.get('zone')}`. Alt: {payload.get('altitude')} ft, ETA: {meta.get('eta')}s."
            }
        else:
            body = alert_data

        try:
            resp = requests.request(
                self.method,
                self.url,
                json=body,
                headers=self.headers,
                timeout=5
            )
            resp.raise_for_status()
        except Exception as exc:
            log.error("Failed to deliver webhook alert: %s", exc)
