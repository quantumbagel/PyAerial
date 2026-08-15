from __future__ import annotations

import pytest

from pyaerial.config.loader import ConfigError, _validate_cross_references, _webhook_url_allowed
from pyaerial.config.schema import AlertActionConfig, ReceiverConfig
from helpers import make_config, make_rule
from pyaerial.config.schema import ZoneConfig


def test_unknown_receiver_rejected():
    config = make_config(receivers={"x": ReceiverConfig(type="not-a-receiver")})
    with pytest.raises(ConfigError, match="unknown type"):
        _validate_cross_references(config, "config.yaml")


def test_unknown_alerter_rejected():
    from pathlib import Path

    config = make_config(
        zones={
            "pad": ZoneConfig(
                coordinates=[[35.72, -78.70], [35.73, -78.70], [35.73, -78.69]],
                rules=[
                    make_rule(
                        name="warn",
                    )
                ],
            )
        }
    )
    config.zones["pad"].rules[0].on_activate = [
        AlertActionConfig(method="discord", options={})
    ]
    with pytest.raises(ConfigError, match="unknown alerter"):
        _validate_cross_references(config, Path("config.yaml"))


def test_webhook_url_rules():
    assert _webhook_url_allowed("https://example.com/hook")
    assert _webhook_url_allowed("http://localhost:8080/hook")
    assert _webhook_url_allowed("http://127.0.0.1/hook")
    assert not _webhook_url_allowed("http://169.254.169.254/")
    assert not _webhook_url_allowed("ftp://example.com")
