from __future__ import annotations

import logging

import pytest
import requests

from pyaerial.alerters.webhook import WebhookAlerter
from pyaerial.constants import STORE_ALT

_SECRET_URL = "https://example.com/api/webhooks/123/SECRET-TOKEN"
_META = {"icao": "abc123", "type": "warn", "zone": "pad", "eta": 10}


class _Resp:
    def __init__(self, status: int, headers: dict | None = None):
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _alerter(url: str = _SECRET_URL, **options) -> WebhookAlerter:
    return WebhookAlerter({"url": url, **options})


def test_configure_rejects_localhost_prefix_bypass():
    with pytest.raises(ValueError, match="https"):
        WebhookAlerter({"url": "http://localhost.attacker.example/hook"})
    with pytest.raises(ValueError, match="https"):
        WebhookAlerter({"url": "http://localhost@evil.example/hook"})


def test_configure_accepts_local_http_and_https():
    WebhookAlerter({"url": "http://127.0.0.1:8080/hook"})
    WebhookAlerter({"url": "https://discord.com/api/webhooks/x"})


def test_configure_restricts_method():
    assert _alerter(method="put").http_method == "PUT"
    with pytest.raises(ValueError, match="POST or PUT"):
        _alerter(method="DELETE")
    with pytest.raises(ValueError, match="POST or PUT"):
        _alerter(method="GET")


def _scripted(monkeypatch, outcomes: list) -> tuple[WebhookAlerter, list, list]:
    """Make the alerter's session return/raise ``outcomes`` in order."""
    alerter = _alerter()
    calls: list[dict] = []
    sleeps: list[float] = []

    def _request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(alerter._session, "request", _request)
    monkeypatch.setattr(
        "pyaerial.alerters.webhook.time.sleep", lambda delay: sleeps.append(delay)
    )
    return alerter, calls, sleeps


def test_alert_retries_then_succeeds(monkeypatch):
    alerter, calls, sleeps = _scripted(
        monkeypatch,
        [requests.ConnectionError("down"), _Resp(503), _Resp(204)],
    )
    alerter.alert(_META, {})
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]
    assert all(call["allow_redirects"] is False for call in calls)
    assert calls[0]["method"] == "POST"


def test_alert_gives_up_after_retries(monkeypatch):
    alerter, calls, sleeps = _scripted(monkeypatch, [requests.Timeout("nope")] * 3)
    alerter.alert(_META, {})
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]


def test_redirect_is_a_failure_and_not_followed(monkeypatch):
    alerter, calls, sleeps = _scripted(
        monkeypatch, [_Resp(302, {"Location": "http://169.254.169.254/"})]
    )
    alerter.alert(_META, {})
    assert len(calls) == 1
    assert sleeps == []


def test_client_errors_are_not_retried(monkeypatch):
    alerter, calls, sleeps = _scripted(monkeypatch, [_Resp(404)])
    alerter.alert(_META, {})
    assert len(calls) == 1
    assert sleeps == []


def test_429_honours_bounded_retry_after(monkeypatch):
    alerter, calls, sleeps = _scripted(
        monkeypatch,
        [
            _Resp(429, {"Retry-After": "2"}),
            _Resp(429, {"Retry-After": "9999"}),
            _Resp(200),
        ],
    )
    alerter.alert(_META, {})
    assert len(calls) == 3
    assert sleeps == [2.0, 30.0]


def test_logs_never_contain_webhook_secret(monkeypatch, caplog):
    error = requests.ConnectionError(f"Max retries exceeded with url: {_SECRET_URL}")
    alerter, _calls, _sleeps = _scripted(monkeypatch, [error, _Resp(500), _Resp(401)])
    with caplog.at_level(logging.DEBUG, logger="pyaerial.alerter.webhook"):
        alerter.alert(_META, {})
    text = caplog.text
    assert "https://example.com" in text
    assert "SECRET-TOKEN" not in text
    assert "/api/webhooks" not in text
    assert "ConnectionError" in text


def test_close_releases_session():
    alerter = _alerter()
    alerter.close()


def test_shorthand_hex_colour_expands_for_discord(monkeypatch):
    """The schema accepts #rgb; Discord needs the full 24-bit value."""
    alerter, calls, _sleeps = _scripted(monkeypatch, [_Resp(204), _Resp(204)])
    alerter.payload_format = "discord"
    alerter.alert({**_META, "color": "#f00"}, {})
    alerter.alert({**_META, "color": "#ef4444"}, {})
    assert calls[0]["json"]["embeds"][0]["color"] == 0xFF0000
    assert calls[1]["json"]["embeds"][0]["color"] == 0xEF4444


def test_format_is_case_insensitive_and_validated():
    assert _alerter(format="Discord").payload_format == "discord"
    assert _alerter(format=" SLACK ").payload_format == "slack"
    assert _alerter().payload_format == "json"
    with pytest.raises(ValueError, match="json, discord, or slack"):
        _alerter(format="discrod")


def test_long_names_stay_within_platform_title_limits(monkeypatch):
    meta = {
        **_META,
        "zone": "z" * 200,
        "type": "r" * 200,
        "reason": {"hook": "while_active"},
    }
    alerter, calls, _sleeps = _scripted(monkeypatch, [_Resp(204), _Resp(204)])
    alerter.payload_format = "discord"
    alerter.alert(meta, {})
    alerter.payload_format = "slack"
    alerter.alert(meta, {})
    assert len(calls[0]["json"]["embeds"][0]["title"]) <= 256
    assert len(calls[1]["json"]["blocks"][0]["text"]["text"]) <= 150


def test_title_names_the_hook_readably(monkeypatch):
    alerter, calls, _sleeps = _scripted(monkeypatch, [_Resp(204)])
    alerter.payload_format = "discord"
    alerter.alert({**_META, "reason": {"hook": "while_active"}}, {})
    assert calls[0]["json"]["embeds"][0]["title"].endswith("(While active)")


# --- Chat formats label GNSS height ------------------------------------------


def test_chat_formats_label_gnss_height(monkeypatch):
    class _Resp:
        status_code = 204
        headers: dict = {}

        def close(self) -> None:
            pass

    bodies: list[dict] = []
    meta = {"icao": "abc123", "type": "warn", "zone": "pad"}
    payload = {STORE_ALT: 304.8, "altitude_source": "gnss"}
    for fmt in ("discord", "slack"):
        alerter = WebhookAlerter({"url": "https://example.com/h", "format": fmt})
        monkeypatch.setattr(
            alerter._session,
            "request",
            lambda *a, **kw: bodies.append(kw["json"]) or _Resp(),
        )
        alerter.alert(meta, payload)
    discord_alt = next(
        f for f in bodies[0]["embeds"][0]["fields"] if f["name"] == "Altitude"
    )
    assert discord_alt["value"] == "1000 ft (GNSS)"
    assert "*Altitude:* 1000 ft (GNSS)" in str(bodies[1]["blocks"])
