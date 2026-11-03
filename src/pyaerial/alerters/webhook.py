import logging
import math
import threading
import time
from urllib.parse import urlsplit

import requests

from pyaerial.alerters import Alerter, register_alerter
from pyaerial.alerters.contract import build_alert_body, ended_by_of, event_of
from pyaerial.units import KMH_TO_KT, M_TO_FT, MPS_TO_FT_PER_MIN
from pyaerial.urls import redact_url, webhook_url_allowed

log = logging.getLogger("pyaerial.alerter.webhook")
_WEBHOOK_ATTEMPTS = 3
_WEBHOOK_RETRY_BACKOFF = 0.5
_WEBHOOK_TIMEOUT = 5
_WEBHOOK_MAX_RETRY_AFTER = 30.0
_WEBHOOK_METHODS = frozenset({"POST", "PUT"})
_WEBHOOK_FORMATS = frozenset({"json", "discord", "slack"})
# Platform limits; an over-long title makes the whole message a 400.
_DISCORD_TITLE_MAX = 256
_DISCORD_FOOTER_MAX = 2048
_SLACK_HEADER_MAX = 150
_SLACK_TEXT_MAX = 3000


class _RedactWebhookSecrets(logging.Filter):
    """Strip configured webhook paths/queries from third-party log records.

    At ``logging.level: debug`` urllib3 logs every request line, e.g.
    ``"POST /api/webhooks/<id>/<token> HTTP/1.1" 204``. The token is the
    credential, so it is replaced before any handler sees the record.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._replacements: dict[str, str] = {}
        self._ordered: list[tuple[str, str]] = []

    def register(self, url: str) -> None:
        parts = urlsplit(url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        if target in ("", "/"):
            return
        with self._lock:
            self._replacements[url] = f"{redact_url(url)}/<redacted>"
            self._replacements[target] = "/<redacted>"
            # Longest first so the full URL wins over its own path.
            self._ordered = sorted(
                self._replacements.items(), key=lambda kv: len(kv[0]), reverse=True
            )

    def filter(self, record: logging.LogRecord) -> bool:
        ordered = self._ordered
        if not ordered:
            return True
        try:
            message = record.getMessage()
        except Exception:  # malformed record; leave it to logging
            return True
        redacted = message
        for secret, replacement in ordered:
            if secret in redacted:
                redacted = redacted.replace(secret, replacement)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


_SECRET_FILTER = _RedactWebhookSecrets()
# Logger filters are not inherited, so attach to each urllib3 module logger.
for _name in (
    "urllib3.connectionpool",
    "urllib3.connection",
    "urllib3.poolmanager",
    "urllib3.response",
    "urllib3.util.retry",
):
    logging.getLogger(_name).addFilter(_SECRET_FILTER)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _alert_ref(meta: dict) -> str:
    """``alert <id>`` line so chat readers can pair activate with deactivate."""
    alert_id = meta.get("alert_id")
    return f"alert {alert_id}" if alert_id else ""


def _altitude_text(alt: float | None, payload: dict) -> str:
    if alt is None:
        return "N/A"
    # GNSS height is geometric (WGS84), not the baro altitude pilots use.
    suffix = " (GNSS)" if payload.get("altitude_source") == "gnss" else ""
    return f"{int(alt * M_TO_FT)} ft{suffix}"


_EVENT_LABELS = {
    "activate": "Activated",
    "while_active": "Still active",
    "deactivate": "Ended",
}
_ENDED_BY_TEXT = {
    "cleared": "conditions cleared",
    "lost": "aircraft lost",
    "shutdown": "engine stopped",
    "restart": "engine restarted",
}
_EVENT_VERBS = {
    "activate": "met",
    "while_active": "still meets",
    "deactivate": "no longer meets",
}


def _event_label(meta: dict) -> str:
    """``Activated`` / ``Still active`` / ``Ended (conditions cleared)``."""
    event = event_of(meta)
    label = _EVENT_LABELS.get(event, event.replace("_", " ").capitalize())
    ended_by = ended_by_of(meta)
    if event == "deactivate" and ended_by:
        label = f"{label} ({_ENDED_BY_TEXT.get(ended_by, ended_by)})"
    return label


def _event_title(meta: dict, callsign: str, icao: str, zone: str, rule: str) -> str:
    # Unchanged from 0.x ("... (While active)"); the event is spelled out in
    # the Event field / description.
    event = event_of(meta)
    hook = f" ({event.replace('_', ' ').capitalize()})" if event else ""
    return f'{callsign} ({icao}) triggered "{zone}/{rule}"{hook}'


def _event_sentence(meta: dict, who: str, zone: str, rule: str) -> str:
    verb = _EVENT_VERBS.get(event_of(meta), "met")
    return f'{who} {verb} the criteria for zone "{zone}", rule "{rule}".'


def _retry_after_seconds(value: object, fallback: float) -> float:
    """Seconds from a numeric ``Retry-After`` header, bounded; else ``fallback``."""
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return fallback
    if math.isnan(seconds) or seconds < 0:
        return fallback
    return min(seconds, _WEBHOOK_MAX_RETRY_AFTER)


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
    clean = hex_str.strip().lstrip("#")
    if len(clean) == 3:  # CSS shorthand: #f00 is #ff0000
        clean = "".join(ch * 2 for ch in clean)
    if len(clean) != 6:
        return default
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
        _SECRET_FILTER.register(url)
        headers = arguments.get("headers") or {}
        if not isinstance(headers, dict):
            raise ValueError("webhook headers must be an object of string keys")
        self.headers = {str(key): str(value) for key, value in headers.items()}
        method = str(arguments.get("method") or "POST").strip().upper()
        if method not in _WEBHOOK_METHODS:
            raise ValueError("webhook method must be POST or PUT")
        self.http_method = method
        payload_format = str(arguments.get("format") or "json").strip().lower()
        if payload_format not in _WEBHOOK_FORMATS:
            raise ValueError("webhook format must be json, discord, or slack")
        self.payload_format = payload_format
        # Webhook paths carry secrets; logs only ever see scheme://host.
        self._target = redact_url(url)
        self._session = requests.Session()

    def close(self) -> None:
        session = getattr(self, "_session", None)
        if session is not None:
            session.close()

    def _deliver(self, body: dict) -> None:
        for attempt in range(1, _WEBHOOK_ATTEMPTS + 1):
            delay = _WEBHOOK_RETRY_BACKOFF * (2 ** (attempt - 1))
            try:
                resp = self._session.request(
                    self.http_method,
                    self.url,
                    json=body,
                    headers=self.headers,
                    timeout=_WEBHOOK_TIMEOUT,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                # The exception text embeds the full URL; log only its type.
                failure = type(exc).__name__
            else:
                status = resp.status_code
                retry_after = resp.headers.get("Retry-After")
                resp.close()
                if 200 <= status < 300:
                    return
                if 300 <= status < 400:
                    log.error(
                        "Webhook %s answered HTTP %d (redirects are not followed); "
                        "alert dropped",
                        self._target,
                        status,
                    )
                    return
                if status == 429:
                    delay = _retry_after_seconds(retry_after, delay)
                elif 400 <= status < 500:
                    log.error(
                        "Webhook %s rejected the alert with HTTP %d; not retrying",
                        self._target,
                        status,
                    )
                    return
                failure = f"HTTP {status}"
            if attempt < _WEBHOOK_ATTEMPTS:
                log.warning(
                    "Webhook %s delivery failed (attempt %d/%d, %s); retrying in %.1fs",
                    self._target,
                    attempt,
                    _WEBHOOK_ATTEMPTS,
                    failure,
                    delay,
                )
                time.sleep(delay)
            else:
                log.error(
                    "Failed to deliver webhook alert to %s after %d attempts (%s)",
                    self._target,
                    _WEBHOOK_ATTEMPTS,
                    failure,
                )

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

        if self.payload_format == "discord":
            body = self._format_discord(meta, payload, tracker_links, map_links)
        elif self.payload_format == "slack":
            body = self._format_slack(meta, payload, tracker_links, map_links)
        else:
            # Public contract: pyaerial.alerters.contract (pyaerial.alert/1).
            body = dict(build_alert_body(meta, payload, tracker_links, map_links))

        self._deliver(body)

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

        lat, lon, alt, speed, heading, vert_speed, eta = _alert_telemetry(meta, payload)

        fields = [
            {"name": "Event", "value": _event_label(meta) or "N/A", "inline": False}
        ]

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
                "value": _altitude_text(alt, payload),
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
            "title": _truncate(
                _event_title(meta, callsign, icao, zone, rule_raw), _DISCORD_TITLE_MAX
            ),
            "description": _event_sentence(
                meta, f"Aircraft **{callsign}** (`{icao}`)", zone, rule_raw
            ),
            "fields": fields,
            "color": embed_color,
        }

        photo_url = meta.get("photo_url")
        if photo_url:
            embed["thumbnail"] = {"url": photo_url}
        ref = _alert_ref(meta)
        if ref:
            embed["footer"] = {"text": _truncate(ref, _DISCORD_FOOTER_MAX)}

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

        lat, lon, alt, speed, heading, vert_speed, eta = _alert_telemetry(meta, payload)

        pos_str = (
            f"<{map_links.get('google_maps', f'https://www.google.com/maps?q={lat},{lon}')}|{lat:.5f}, {lon:.5f}>"
            if lat is not None and lon is not None
            else "N/A"
        )
        alt_str = _altitude_text(alt, payload)
        eta_str = f"{int(eta)}s" if eta is not None else "N/A"
        speed_str = f"{speed * KMH_TO_KT:.1f} kt" if speed is not None else "N/A"
        heading_str = f"{heading:.0f}°" if heading is not None else "N/A"
        vspeed_str = (
            f"{vert_speed * MPS_TO_FT_PER_MIN:+.0f} ft/min"
            if vert_speed is not None
            else "N/A"
        )

        label = _event_label(meta)
        slack_text = (f"[{label}] " if label else "") + _event_sentence(
            meta, f"Aircraft `{callsign}` (`{icao}`)", zone, rule_raw
        )

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
                    "text": _truncate(
                        _event_title(meta, callsign, icao, zone, rule_raw),
                        _SLACK_HEADER_MAX,
                    ),
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
        ref = _alert_ref(meta)
        if ref:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {"type": "plain_text", "text": _truncate(ref, _SLACK_TEXT_MAX)}
                    ],
                }
            )

        return {
            "text": slack_text,
            "blocks": blocks,
        }
