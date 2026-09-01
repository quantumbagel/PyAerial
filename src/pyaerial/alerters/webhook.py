import logging
import math
import time

import requests

from pyaerial.alerters import Alerter, register_alerter
from pyaerial.config.loader import webhook_url_allowed
from pyaerial.units import KMH_TO_KT, M_TO_FT, MPS_TO_FT_PER_MIN

log = logging.getLogger("pyaerial.alerter.webhook")
_WEBHOOK_ATTEMPTS = 3
_WEBHOOK_RETRY_BACKOFF = 0.5
_WEBHOOK_TIMEOUT = 5


def build_tracker_links(
    icao: str, callsign: str | None = None, registration: str | None = None
) -> dict[str, str]:
    """Generate direct links to aircraft matching the webapp's external trackers (FlightAware, ADS-B Exchange, RadarBox)."""
    icao_clean = (icao or "").strip().lower()
    callsign_clean = (callsign or "").strip()
    reg_clean = (registration or "").strip()

    links = {}
    if not icao_clean:
        return links

    # FlightAware
    if callsign_clean:
        links["flightaware"] = f"https://flightaware.com/live/flight/{callsign_clean}"
    else:
        links["flightaware"] = f"https://flightaware.com/live/modes/{icao_clean}"

    # ADS-B Exchange
    links["adsbexchange"] = f"https://globe.adsbexchange.com/?icao={icao_clean}"

    # RadarBox
    if reg_clean:
        links["radarbox"] = f"https://www.radarbox.com/data/registration/{reg_clean}"
    else:
        links["radarbox"] = f"https://www.radarbox.com/data/mode-s/{icao_clean}"

    return links


def build_map_links(latitude: float | None, longitude: float | None) -> dict[str, str]:
    """Generate map links for the aircraft's current coordinates."""
    if latitude is None or longitude is None:
        return {}
    try:
        lat_f = float(latitude)
        lon_f = float(longitude)
        if (
            math.isnan(lat_f)
            or math.isnan(lon_f)
            or math.isinf(lat_f)
            or math.isinf(lon_f)
        ):
            return {}
        return {
            "google_maps": f"https://www.google.com/maps?q={lat_f},{lon_f}",
            "openstreetmap": f"https://www.openstreetmap.org/?mlat={lat_f}&mlon={lon_f}&zoom=14",
        }
    except (TypeError, ValueError):
        return {}


def _alert_telemetry(meta: dict, payload: dict) -> tuple:
    return (
        _safe_num(payload.get("latitude")),
        _safe_num(payload.get("longitude")),
        _safe_num(payload.get("altitude")),
        _safe_num(payload.get("speed")),
        _safe_num(payload.get("heading")),
        _safe_num(payload.get("vertical_speed")),
        _safe_num(meta.get("eta")),
    )


def _safe_num(val: object) -> float | int | None:
    if val is None:
        return None
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _hex_to_int(hex_str: str | None, default: int = 15680580) -> int:
    if not hex_str or not isinstance(hex_str, str):
        return default
    clean = hex_str.lstrip("#").strip()
    try:
        return int(clean, 16)
    except ValueError:
        return default


@register_alerter("webhook")
class WebhookAlerter(Alerter):
    """Sends JSON alerts to a configured HTTP webhook endpoint."""

    def configure(self, arguments: dict) -> None:
        self.url = arguments.get("url")
        if not self.url:
            raise ValueError("webhook URL must be provided in alerter options")
        url = str(self.url).strip()
        if not webhook_url_allowed(url):
            raise ValueError(
                "webhook URL must be https (http allowed only for localhost)"
            )
        self.url = url
        self.headers = arguments.get("headers", {})
        self.http_method = arguments.get("method", "POST")
        self.payload_format = arguments.get("format", "json")  # json, discord, slack

    def alert(self, meta: dict, payload: dict) -> None:
        icao = meta.get("icao", "")
        callsign = meta.get("callsign", "")
        registration = meta.get("registration", "")

        lat = _safe_num(payload.get("latitude"))
        lon = _safe_num(payload.get("longitude"))

        tracker_links = meta.get("tracker_links") or build_tracker_links(
            icao, callsign, registration
        )
        map_links = meta.get("map_links") or build_map_links(lat, lon)

        alert_data = {
            **meta,
            "tracker_links": tracker_links,
            "map_links": map_links,
            "telemetry": payload,
        }

        # Format payload for common notification platforms
        if self.payload_format == "discord":
            body = self._format_discord(meta, payload, tracker_links, map_links)
        elif self.payload_format == "slack":
            body = self._format_slack(meta, payload, tracker_links, map_links)
        else:
            body = alert_data

        last_error: BaseException | None = None
        for attempt in range(1, _WEBHOOK_ATTEMPTS + 1):
            try:
                resp = requests.request(
                    self.http_method,
                    self.url,
                    json=body,
                    headers=self.headers,
                    timeout=_WEBHOOK_TIMEOUT,
                )
                resp.raise_for_status()
                return
            except Exception as exc:
                last_error = exc
                if attempt < _WEBHOOK_ATTEMPTS:
                    delay = _WEBHOOK_RETRY_BACKOFF * (2 ** (attempt - 1))
                    log.warning(
                        "Webhook delivery failed (attempt %d/%d); retrying in %.1fs: %s",
                        attempt,
                        _WEBHOOK_ATTEMPTS,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
        log.error(
            "Failed to deliver webhook alert after %d attempts: %s",
            _WEBHOOK_ATTEMPTS,
            last_error,
        )

    def _format_discord(
        self,
        meta: dict,
        payload: dict,
        tracker_links: dict[str, str],
        map_links: dict[str, str],
    ) -> dict:
        icao = (meta.get("icao") or "").upper()
        callsign = meta.get("callsign") or "Unknown"
        zone = meta.get("zone", "Unknown")
        reason = meta.get("reason")
        hook = reason.get("hook") if isinstance(reason, dict) else None
        hook_str = f" ({hook.capitalize()})" if hook else ""

        lat, lon, alt, speed, heading, vert_speed, eta = _alert_telemetry(
            meta, payload
        )

        fields = []

        # Position Field
        if lat is not None and lon is not None:
            gmap_url = map_links.get(
                "google_maps", f"https://www.google.com/maps?q={lat},{lon}"
            )
            fields.append(
                {
                    "name": "Location (Lat, Lon)",
                    "value": f"[{lat:.5f}, {lon:.5f}]({gmap_url})",
                    "inline": True,
                }
            )
        else:
            fields.append(
                {"name": "Location (Lat, Lon)", "value": "N/A", "inline": True}
            )

        fields.append(
            {
                "name": "Altitude",
                "value": f"{int(alt * M_TO_FT)} ft" if alt is not None else "N/A",
                "inline": True,
            }
        )
        fields.append(
            {
                "name": "ETA",
                "value": f"{int(eta)}s" if eta is not None else "N/A",
                "inline": True,
            }
        )

        # Telemetry Summary
        speed_str = f"{speed * KMH_TO_KT:.1f} kt" if speed is not None else "N/A"
        heading_str = f"{heading:.0f}°" if heading is not None else "N/A"
        vspeed_str = (
            f"{vert_speed * MPS_TO_FT_PER_MIN:+.0f} ft/min"
            if vert_speed is not None
            else "N/A"
        )
        fields.append(
            {
                "name": "Telemetry (Speed / Heading / VertSpeed)",
                "value": f"{speed_str} | {heading_str} | {vspeed_str}",
                "inline": False,
            }
        )

        # Aircraft Details
        details = []
        if meta.get("registration"):
            details.append(f"**Reg:** {meta['registration']}")
        if meta.get("model") or meta.get("aircraft_type"):
            model_str = (
                f"{meta.get('model', '')} ({meta.get('aircraft_type', '')})".strip(
                    " ()"
                )
            )
            details.append(f"**Model:** {model_str}")
        if meta.get("manufacturer"):
            details.append(f"**Mfr:** {meta['manufacturer']}")
        if meta.get("owner"):
            details.append(f"**Owner:** {meta['owner']}")

        if details:
            fields.append(
                {"name": "Aircraft Info", "value": " • ".join(details), "inline": False}
            )

        # Plane Trackers Field
        tracker_names = {
            "flightaware": "FlightAware",
            "adsbexchange": "ADS-B Exchange",
            "radarbox": "RadarBox",
        }
        tracker_md = [
            f"[{tracker_names.get(k, k.capitalize())}]({v})"
            for k, v in tracker_links.items()
        ]
        if tracker_md:
            fields.append(
                {
                    "name": "Plane Trackers",
                    "value": " | ".join(tracker_md),
                    "inline": False,
                }
            )

        embed_color = _hex_to_int(meta.get("color"), default=15680580)

        rule_raw = meta.get("type", "unknown")
        embed = {
            "title": f'{callsign} ({icao}) triggered "{zone}/{rule_raw}"{hook_str}',
            "description": f'Aircraft **{callsign}** (`{icao}`) met criteria for zone "{zone}", rule "{rule_raw}".',
            "fields": fields,
            "color": embed_color,
        }

        photo_url = meta.get("photo_url")
        if photo_url:
            embed["thumbnail"] = {"url": photo_url}

        return {"embeds": [embed]}

    def _format_slack(
        self,
        meta: dict,
        payload: dict,
        tracker_links: dict[str, str],
        map_links: dict[str, str],
    ) -> dict:
        icao = (meta.get("icao") or "").upper()
        callsign = meta.get("callsign") or "Unknown"
        zone = meta.get("zone", "Unknown")
        rule_raw = meta.get("type", "unknown")
        reason = meta.get("reason")
        hook = reason.get("hook") if isinstance(reason, dict) else None
        hook_str = f" ({hook.capitalize()})" if hook else ""

        lat, lon, alt, speed, heading, vert_speed, eta = _alert_telemetry(
            meta, payload
        )

        pos_str = (
            f"<{map_links.get('google_maps', f'https://www.google.com/maps?q={lat},{lon}')}|{lat:.5f}, {lon:.5f}>"
            if lat is not None and lon is not None
            else "N/A"
        )
        alt_str = f"{int(alt * M_TO_FT)} ft" if alt is not None else "N/A"
        eta_str = f"{int(eta)}s" if eta is not None else "N/A"
        speed_str = f"{speed * KMH_TO_KT:.1f} kt" if speed is not None else "N/A"
        heading_str = f"{heading:.0f}°" if heading is not None else "N/A"
        vspeed_str = (
            f"{vert_speed * MPS_TO_FT_PER_MIN:+.0f} ft/min"
            if vert_speed is not None
            else "N/A"
        )

        slack_text = f'Aircraft `{callsign}` (`{icao}`) met criteria for zone "{zone}", rule "{rule_raw}".'

        fields = [
            {"type": "mrkdwn", "text": f"*Aircraft:* `{callsign}` ({icao})"},
            {"type": "mrkdwn", "text": f"*Zone:* `{zone}`"},
            {"type": "mrkdwn", "text": f"*Lat/Lon:* {pos_str}"},
            {"type": "mrkdwn", "text": f"*Altitude:* {alt_str}"},
            {"type": "mrkdwn", "text": f"*ETA:* {eta_str}"},
            {"type": "mrkdwn", "text": f"*Speed:* {speed_str}"},
            {"type": "mrkdwn", "text": f"*Heading:* {heading_str}"},
            {"type": "mrkdwn", "text": f"*Vert Speed:* {vspeed_str}"},
        ]

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f'{callsign} ({icao}) triggered "{zone}/{rule_raw}"{hook_str}',
                },
            },
            {
                "type": "section",
                "fields": fields,
            },
        ]

        tracker_names = {
            "flightaware": "FlightAware",
            "adsbexchange": "ADS-B Exchange",
            "radarbox": "RadarBox",
        }
        tracker_slack = [
            f"<{v}|{tracker_names.get(k, k.capitalize())}>"
            for k, v in tracker_links.items()
        ]
        if tracker_slack:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Plane Trackers:* " + " | ".join(tracker_slack),
                    },
                }
            )

        return {
            "text": slack_text,
            "blocks": blocks,
        }
