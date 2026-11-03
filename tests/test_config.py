from __future__ import annotations

import pytest
from pydantic import ValidationError

from helpers import make_config, make_rule
from pyaerial import cli
from pyaerial.config.loader import (
    ConfigError,
    _validate_cross_references,
    load_config,
)
from pyaerial.config.schema import (
    AlertActionConfig,
    Config,
    DatabaseConfig,
    LoggingConfig,
    ReceiverConfig,
    TrackingConfig,
    WebConfig,
    ZoneConfig,
)
from pyaerial.urls import webhook_url_allowed


def test_when_requires_a_spatial_field():
    with pytest.raises(ValidationError, match="eta, distance, dist, or proximity"):
        make_rule(altitude={"max": 2000})


def test_activation_hold_uses_dwell_when_hysteresis_is_zero():
    rule = make_rule(dwell_seconds=60, hysteresis_seconds=0)
    assert rule.activation_hold_seconds() == 60
    rule = make_rule(dwell_seconds=60, hysteresis_seconds=15)
    assert rule.activation_hold_seconds() == 15


def test_zone_requires_coordinates_or_file():
    with pytest.raises(ValidationError, match="coordinates or file"):
        ZoneConfig(rules=[make_rule()])


def test_dump1090_format_is_passed_to_receiver_arguments():
    avr = ReceiverConfig(type="dump1090", host="localhost", port=30002)
    args = avr.receiver_arguments()
    assert args["tcp_connection_ip"] == "localhost"
    assert args["tcp_connection_port"] == 30002
    assert "format" not in args

    beast = ReceiverConfig(
        type="dump1090", host="localhost", port=30005, format="beast"
    )
    args = beast.receiver_arguments()
    assert args["format"] == "beast"
    assert args["tcp_connection_port"] == 30005


def test_dump1090_unknown_format_rejected():
    with pytest.raises(ValidationError, match="avr, raw, beast, binary"):
        ReceiverConfig(type="dump1090", host="localhost", format="sbs")


def test_empty_receivers_rejected():
    from pyaerial.config.schema import Config, HomeConfig

    with pytest.raises(ValidationError):
        Config(
            home=HomeConfig(latitude=35.7, longitude=-78.7),
            receivers={},
        )


def test_remember_planes_default_is_two_minutes():
    from pyaerial.config.schema import TrackingConfig

    assert TrackingConfig().remember_planes == 120


def test_unknown_receiver_rejected():
    config = make_config(receivers={"x": ReceiverConfig(type="not-a-receiver")})
    with pytest.raises(ConfigError, match="unknown type"):
        _validate_cross_references(config, "config.yaml")


def test_dump1090_beast_on_avr_port_rejected():
    config = make_config(
        receivers={
            "main": ReceiverConfig(
                type="dump1090", host="localhost", port=30002, format="beast"
            )
        }
    )
    with pytest.raises(ConfigError, match="Beast binary"):
        _validate_cross_references(config, "config.yaml")


def test_dump1090_avr_on_beast_port_rejected():
    config = make_config(
        receivers={
            "main": ReceiverConfig(
                type="dump1090", host="localhost", port=30005, format="avr"
            )
        }
    )
    with pytest.raises(ConfigError, match="AVR text"):
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
    assert webhook_url_allowed("https://example.com/hook")
    assert webhook_url_allowed("http://localhost:8080/hook")
    assert webhook_url_allowed("http://127.0.0.1/hook")
    assert webhook_url_allowed("http://[::1]/hook")
    assert not webhook_url_allowed("http://169.254.169.254/")
    assert not webhook_url_allowed("ftp://example.com")
    assert not webhook_url_allowed("http://localhost.attacker.example/hook")
    assert not webhook_url_allowed("http://127.0.0.1.attacker.example/hook")
    assert not webhook_url_allowed("http://localhost@evil.example/hook")
    assert not webhook_url_allowed("http://")


_MINIMAL_YAML = """
home:
  latitude: 35.7275
  longitude: -78.6959
receivers:
  main:
    type: dump1090
"""


def test_env_interpolation_default(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        _MINIMAL_YAML
        + """
database:
  path: "${PYAERIAL_TEST_HISTORY:-history.db}"
"""
    )
    config = load_config(cfg_path)
    assert config.database.path == str((tmp_path / "history.db").resolve())


def test_env_interpolation_webhook_url(tmp_path, monkeypatch):
    monkeypatch.setenv("PYAERIAL_WEBHOOK_URL", "https://hooks.example/a")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        _MINIMAL_YAML
        + """
zones:
  pad:
    coordinates: [[35.72, -78.70], [35.73, -78.70], [35.73, -78.69], [35.72, -78.70]]
    rules:
      - name: warn
        when:
          altitude: { max: 2000 }
          eta: { max: 120 }
        dwell_seconds: 1
        on_activate:
          - method: webhook
            options:
              url: "${PYAERIAL_WEBHOOK_URL}"
"""
    )
    config = load_config(cfg_path)
    url = config.zones["pad"].rules[0].on_activate[0].options["url"]
    assert url == "https://hooks.example/a"


def test_env_interpolation_missing_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("PYAERIAL_MISSING_SECRET", raising=False)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        _MINIMAL_YAML
        + """
database:
  path: "${PYAERIAL_MISSING_SECRET}"
"""
    )
    with pytest.raises(ConfigError, match="unset environment variable"):
        load_config(cfg_path)


def test_mongo_database_keys_are_rejected(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        _MINIMAL_YAML
        + """
database:
  uri: mongodb://localhost:27017
"""
    )
    with pytest.raises(ConfigError, match="no longer used"):
        load_config(cfg_path)


def test_web_defaults_are_locked_down():
    from pyaerial.config.schema import WebConfig

    web = WebConfig()
    assert web.origins == []
    assert web.allow_localhost_origins is False
    assert web.enable_docs is False
    assert web.beast_host == "127.0.0.1"
    assert web.beast_max_clients == 16
    assert web.ws_max_inflight <= 2


def test_web_origins_must_be_scheme_host_port():
    from pyaerial.config.schema import WebConfig

    assert WebConfig(origins=["https://portal.example/", "*"]).origins == [
        "https://portal.example",
        "*",
    ]
    with pytest.raises(ValidationError, match="scheme://host"):
        WebConfig(origins=["portal.example"])
    with pytest.raises(ValidationError, match="scheme://host"):
        WebConfig(origins=["https://portal.example/path"])


def test_beast_host_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PYAERIAL_BEAST_HOST", "0.0.0.0")
    monkeypatch.setenv("PYAERIAL_BEAST_PORT", "off")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_MINIMAL_YAML)
    config = load_config(cfg_path)
    assert config.web.beast_host == "0.0.0.0"
    assert config.web.beast_port is None


def test_allowed_hosts_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PYAERIAL_WEB_ALLOWED_HOSTS", "pi.local, tracker.example")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_MINIMAL_YAML)
    config = load_config(cfg_path)
    assert config.web.allowed_hosts == ["pi.local", "tracker.example"]


def test_bad_webhook_url_error_does_not_leak_secret_path(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        _MINIMAL_YAML
        + """
zones:
  pad:
    coordinates: [[35.72, -78.70], [35.73, -78.70], [35.73, -78.69], [35.72, -78.70]]
    rules:
      - name: warn
        when:
          altitude: { max: 2000 }
          eta: { max: 120 }
        dwell_seconds: 1
        on_activate:
          - method: webhook
            options:
              url: "http://hooks.example/api/webhooks/123/SECRET-TOKEN"
"""
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg_path)
    message = str(excinfo.value)
    assert "http://hooks.example" in message
    assert "SECRET-TOKEN" not in message
    assert "/api/webhooks" not in message


# --- Values that used to pass validation but fail later ----------------------


def _write_minimal(tmp_path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(_MINIMAL_YAML + text)
    return path


def test_section_with_only_commented_keys_uses_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("PYAERIAL_LOG_LEVEL", raising=False)
    monkeypatch.delenv("PYAERIAL_BEAST_PORT", raising=False)
    path = _write_minimal(tmp_path, "logging:\n  # file: /var/log/pyaerial.log\nweb:\n")
    config = load_config(path)
    assert config.logging.level == "info"
    assert config.web.beast_port == 30105


def test_env_override_into_empty_section(tmp_path, monkeypatch):
    monkeypatch.setenv("PYAERIAL_LOG_LEVEL", "debug")
    path = _write_minimal(tmp_path, "logging:\n  # level: info\n")
    assert load_config(path).logging.level == "debug"


def test_log_level_is_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setenv("PYAERIAL_LOG_LEVEL", "WARNING")
    assert load_config(_write_minimal(tmp_path, "")).logging.level == "warning"


def test_bad_beast_port_env_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PYAERIAL_BEAST_PORT", "3OO05")
    with pytest.raises(ConfigError, match="PYAERIAL_BEAST_PORT"):
        load_config(_write_minimal(tmp_path, ""))


def test_unreadable_config_paths_raise_config_error(tmp_path):
    with pytest.raises(ConfigError, match="could not read"):
        load_config(tmp_path)
    binary = tmp_path / "config.yaml"
    binary.write_bytes(b"\xff\xfe\x00home: 1")
    with pytest.raises(ConfigError, match="could not read"):
        load_config(binary)


def test_redis_uri_needs_a_redis_scheme():
    assert DatabaseConfig(redis_uri="rediss://:pw@cache:6380/1")
    assert DatabaseConfig(redis_uri="unix:///run/redis.sock")
    with pytest.raises(ValidationError, match="redis://"):
        DatabaseConfig(redis_uri="localhost:6379")


def test_receiver_port_must_be_a_tcp_port():
    with pytest.raises(ValidationError):
        ReceiverConfig(type="dump1090", port=99999)
    with pytest.raises(ValidationError):
        ReceiverConfig(type="dump1090", port=0)


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_non_finite_numbers_are_rejected(value):
    with pytest.raises(ValidationError):
        TrackingConfig(hz=value)
    with pytest.raises(ValidationError):
        make_rule(distance={"max": value})


def test_inverted_range_is_rejected_except_for_heading():
    with pytest.raises(ValidationError, match="min .* greater than max"):
        make_rule(altitude={"min": 1000, "max": 500}, eta={"max": 60})
    # Heading ranges wrap through north.
    rule = make_rule(heading={"min": 350, "max": 10}, eta={"max": 60})
    assert rule.when["heading"].minimum == 350


def test_trusted_proxies_must_be_ips_or_networks():
    web = WebConfig(trusted_proxies=["10.0.0.1", "172.16.0.0/12", "::1", "*"])
    assert web.trusted_proxies == ["10.0.0.1", "172.16.0.0/12", "::1", "*"]
    for bad in ("proxy.local", "10.0.0.1/8"):
        with pytest.raises(ValidationError, match="trusted_proxies"):
            WebConfig(trusted_proxies=[bad])


_WEBHOOK_YAML = """
zones:
  pad:
    coordinates: [[35.72, -78.70], [35.73, -78.70], [35.73, -78.69]]
    rules:
      - name: warn
        when: {eta: {max: 120}}
        dwell_seconds: 1
        on_activate:
          - method: webhook
            options: {url: "https://hooks.example/secret", %s}
"""


@pytest.mark.parametrize(
    ("option", "message"),
    [("format: discrod", "format"), ("headers: [x]", "headers")],
)
def test_webhook_options_are_validated_at_load(tmp_path, option, message):
    """The engine would drop every alert for this action; fail at load instead."""
    with pytest.raises(ConfigError, match=message) as excinfo:
        load_config(_write_minimal(tmp_path, _WEBHOOK_YAML % option))
    assert "secret" not in str(excinfo.value)
    assert load_config(_write_minimal(tmp_path, _WEBHOOK_YAML % "format: slack"))


def test_cross_network_url_needs_icao_placeholder():
    from pyaerial.config.schema import CrossNetworkConfig

    assert CrossNetworkConfig(url="https://api.example/v2/hex/{icao}")
    # Without {icao} every aircraft is compared with the same response.
    for bad in ("https://api.example/v2/hex/", "ftp://api.example/{icao}"):
        with pytest.raises(ValidationError, match="url"):
            CrossNetworkConfig(url=bad)


def test_allowed_hosts_are_bare_hostnames():
    assert WebConfig(allowed_hosts=["Tracker.Example", "*.lan", "10.0.0.5"])
    for bad in ("tracker.example:8443", "https://tracker.example"):
        with pytest.raises(ValidationError, match="allowed_hosts"):
            WebConfig(allowed_hosts=[bad])


# --- Strict options, env-override blame and ${file:...} ----------------------


_HOME_YAML = "home: {latitude: 35.7275, longitude: -78.6959}\n"


_RECEIVER_YAML = "receivers: {main: {type: dump1090}}\n"


_ZONE_YAML = """zones:
  pad:
    coordinates: [[35.72, -78.70], [35.73, -78.70], [35.73, -78.69]]
    rules:
      - name: warn
        when: {eta: {max: 60}}
        dwell_seconds: 1
        on_activate: [%s]
"""


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("PYAERIAL_HZ", "PYAERIAL_LOG_LEVEL", "PYAERIAL_BEAST_PORT"):
        monkeypatch.delenv(name, raising=False)


def _write_config(tmp_path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


# --- contracts other agents rely on ------------------------------------------


def test_schema_contracts():
    assert TrackingConfig().max_flight_seconds == 14400.0
    assert TrackingConfig(max_flight_seconds=0).max_flight_seconds == 0
    with pytest.raises(ValueError):
        TrackingConfig(max_flight_seconds=-1)
    config = Config(
        home={"latitude": 1, "longitude": 2}, receivers={"m": {"type": "dump1090"}}
    )
    assert config.enrichment.enabled is True
    assert (
        Config(
            home={"latitude": 1, "longitude": 2},
            receivers={"m": {"type": "dump1090"}},
            enrichment={"enabled": False},
        ).enrichment.enabled
        is False
    )
    # Not 30005: that is where the engine reads dump1090 by default.
    assert WebConfig().beast_port == 30105


def test_warn_is_an_alias_for_warning(monkeypatch, tmp_path):
    assert LoggingConfig(level="WARN").level == "warning"
    monkeypatch.setenv("PYAERIAL_LOG_LEVEL", "warn")
    assert load_config(_write_config(tmp_path, _HOME_YAML + _RECEIVER_YAML)).logging.level == "warning"


# --- unknown option keys -----------------------------------------------------


@pytest.mark.parametrize(
    ("receivers", "message"),
    [
        (
            "{d: {type: dump1090, options: {fromat: beast}}}",
            "receivers.d.options: unknown option 'fromat'",
        ),
        (
            "{r: {type: replay, options: {path: c.raw, sped: 2}}}",
            "unknown option 'sped'",
        ),
        (
            "{a: {type: aoa, port: 30010, options: {bearing_ofset: 3}}}",
            "unknown option 'bearing_ofset'",
        ),
    ],
)
def test_unknown_receiver_options_are_rejected(tmp_path, receivers, message):
    (tmp_path / "c.raw").write_text("")
    with pytest.raises(ConfigError, match=message) as excinfo:
        load_config(_write_config(tmp_path, _HOME_YAML + f"receivers: {receivers}\n"))
    assert "did you mean" in str(excinfo.value)


def test_known_receiver_options_are_accepted(tmp_path):
    (tmp_path / "c.raw").write_text("")
    config = load_config(
        _write_config(
            tmp_path,
            _HOME_YAML
            + "receivers:\n"
            + "  d: {type: dump1090, options: {format: beast}}\n"
            + "  r: {type: replay, options: {path: c.raw, speed: 2, loop: false, interval: 0}}\n"
            + "  a: {type: aoa, port: 30010, options: {bearing_offset: 3, array_axis: 90}}\n",
        )
    )
    assert set(config.receivers) == {"d", "r", "a"}


@pytest.mark.parametrize(
    ("action", "message"),
    [
        (
            "{method: webhook, options: {url: 'https://h.example/x', fromat: discord}}",
            "unknown option 'fromat'",
        ),
        ("{method: print, options: {verbose: true}}", "unknown option 'verbose'"),
    ],
)
def test_unknown_alerter_options_are_rejected(tmp_path, action, message):
    with pytest.raises(ConfigError, match=message):
        load_config(_write_config(tmp_path, _HOME_YAML + _RECEIVER_YAML + _ZONE_YAML % action))


# --- env overrides name the variable ----------------------------------------


@pytest.mark.parametrize(
    ("var", "value", "setting"),
    [
        ("PYAERIAL_HZ", "abc", "tracking.hz"),
        ("PYAERIAL_LOG_LEVEL", "loud", "logging.level"),
    ],
)
def test_env_override_errors_name_the_variable(
    tmp_path, monkeypatch, var, value, setting
):
    monkeypatch.setenv(var, value)
    with pytest.raises(ConfigError) as excinfo:
        load_config(_write_config(tmp_path, _HOME_YAML + _RECEIVER_YAML))
    message = str(excinfo.value)
    assert f"{var}={value!r}" in message
    assert setting in message


# --- ${file:...} -------------------------------------------------------------


def test_file_interpolation_reads_secrets(tmp_path):
    (tmp_path / "hook").write_text("https://discord.example/api/webhooks/1/TOKEN\n")
    config = load_config(
        _write_config(
            tmp_path,
            _HOME_YAML
            + _RECEIVER_YAML
            + _ZONE_YAML
            % "{method: webhook, options: {url: '${file:hook}', format: discord}}",
        )
    )
    url = config.zones["pad"].rules[0].on_activate[0].options["url"]
    assert url == "https://discord.example/api/webhooks/1/TOKEN"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "cannot read"),
        ("", "is empty"),
        ("http://h.example/api/SECRET-TOKEN", "must be https"),
    ],
)
def test_file_interpolation_errors_never_echo_contents(
    tmp_path, capsys, content, message
):
    if content is not None:
        (tmp_path / "hook").write_text(content)
    path = _write_config(
        tmp_path,
        _HOME_YAML + _RECEIVER_YAML + _ZONE_YAML % "{method: webhook, options: {url: '${file:hook}'}}",
    )
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["validate", "-c", str(path)])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert message in err
    assert "SECRET-TOKEN" not in err and "/api/" not in err


# --- Beast feedback loop -----------------------------------------------------


def test_default_beast_port_is_not_the_engines_dump1090_port():
    from pyaerial.receivers.dump1090 import Dump1090Receiver

    receiver = Dump1090Receiver("main", lambda *_: None, {})
    assert receiver.port == 30005 != WebConfig().beast_port


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_receiver_reading_the_portals_own_beast_port_is_rejected(tmp_path, host):
    text = _HOME_YAML + f"receivers: {{main: {{type: dump1090, host: {host}, port: 30005}}}}\n"
    text += "web: {beast_port: 30005}\n"
    with pytest.raises(ConfigError, match="its own re-served output"):
        load_config(_write_config(tmp_path, text))


def test_remote_receiver_on_the_same_port_is_fine(tmp_path):
    text = _HOME_YAML + "receivers: {main: {type: dump1090, host: feeder.lan, port: 30005}}\n"
    text += "web: {beast_port: 30005}\n"
    assert load_config(_write_config(tmp_path, text)).web.beast_port == 30005


# --- web.beast_port: 0 ---------------------------------------------------------


def test_beast_port_zero_is_rejected_with_a_hint():
    with pytest.raises(ValidationError, match="use null"):
        WebConfig(beast_port=0)
    assert WebConfig(beast_port=None).beast_port is None
    assert WebConfig(beast_port=1).beast_port == 1


def test_beast_port_env_zero_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("PYAERIAL_BEAST_PORT", "0")
    path = tmp_path / "config.yaml"
    path.write_text(
        "home: {latitude: 35.7, longitude: -78.6}\nreceivers: {main: {type: dump1090}}\n"
    )
    with pytest.raises(ConfigError, match="PYAERIAL_BEAST_PORT"):
        load_config(path)
    monkeypatch.setenv("PYAERIAL_BEAST_PORT", "off")
    assert load_config(path).web.beast_port is None
