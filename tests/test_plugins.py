"""Plugin contract and entry-point discovery for receivers and alerters."""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import pyaerial.alerters as alerters_mod
import pyaerial.receivers as receivers_mod
from pyaerial import cli
from pyaerial.alerters import Alerter
from pyaerial.config.loader import ConfigError, load_config
import pyaerial.plugins as plugins_mod
from pyaerial.plugins import BUILTIN, PluginRegistry
from pyaerial.receivers import Receiver, ReceiverEndpoint
from pyaerial.receivers.dump1090 import Dump1090Receiver

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIST = "pyaerial-test-plugin"


# --- a third-party distribution's plugins ---------------------------------------


class FakeSdrReceiver(Receiver):
    known_options = frozenset({"device", "gain"})

    @classmethod
    def validate_options(
        cls, options: dict[str, Any], *, config_dir: Path
    ) -> list[str]:
        problems = []
        gain = options.get("gain", 0)
        if not isinstance(gain, (int, float)) or not 0 <= gain <= 50:
            problems.append("gain must be 0-50")
        device = Path(str(options.get("device", ""))).expanduser()
        if not device.is_absolute():
            device = config_dir / device
        options["device"] = str(device)  # normalized in place
        if not device.is_file():
            problems.append(f"options.device: not found: {device}")
        return problems

    def run(self) -> str | None:
        return None


class FakePagerAlerter(Alerter):
    known_options = frozenset({"channel", "priority"})

    @classmethod
    def validate_options(
        cls, options: dict[str, Any], *, config_dir: Path
    ) -> list[str]:
        return [] if options.get("channel") else ["fakepager needs options.channel"]

    def alert(self, meta: dict, payload: dict) -> None:
        pass


class NoOptionsReceiver(Receiver):
    """Forgets to declare known_options."""

    def run(self) -> str | None:
        return None


class _EntryPoint:
    def __init__(self, name: str, target: Any, dist: str = PLUGIN_DIST) -> None:
        self.name = name
        self._target = target
        self.dist = SimpleNamespace(name=dist)

    def load(self) -> Any:
        if isinstance(self._target, BaseException):
            raise self._target
        return self._target


@pytest.fixture
def plugins(monkeypatch):
    """Fresh registries discovering the given entry points instead of metadata."""
    groups: dict[str, list[_EntryPoint]] = {
        "pyaerial.receivers": [],
        "pyaerial.alerters": [],
    }

    def _entry_points(*, group: str) -> list[_EntryPoint]:
        return list(groups.get(group, []))

    monkeypatch.setattr(plugins_mod, "entry_points", _entry_points)
    old_receivers, old_alerters = receivers_mod.RECEIVERS, alerters_mod.ALERTERS
    monkeypatch.setattr(
        receivers_mod,
        "RECEIVERS",
        PluginRegistry(
            "receiver", "pyaerial.receivers", Receiver, dict(old_receivers._builtins)
        ),
    )
    monkeypatch.setattr(
        alerters_mod,
        "ALERTERS",
        alerters_mod._AlerterRegistry(
            "alerter", "pyaerial.alerters", Alerter, dict(old_alerters._builtins)
        ),
    )
    return groups


_HOME = "home: {latitude: 35.7275, longitude: -78.6959}\n"
_ZONE = """zones:
  pad:
    coordinates: [[35.72, -78.70], [35.73, -78.70], [35.73, -78.69]]
    rules:
      - name: warn
        when: {eta: {max: 60}}
        dwell_seconds: 1
        on_activate: [%s]
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def _fake_plugins(plugins) -> None:
    plugins["pyaerial.receivers"].append(_EntryPoint("fakesdr", FakeSdrReceiver))
    plugins["pyaerial.alerters"].append(_EntryPoint("fakepager", FakePagerAlerter))


# --- end to end through load_config ---------------------------------------------


def test_third_party_plugins_validate_end_to_end(plugins, tmp_path):
    _fake_plugins(plugins)
    (tmp_path / "dev.bin").write_text("")
    config = load_config(
        _write(
            tmp_path,
            _HOME
            + "receivers: {sdr: {type: fakesdr, options: {device: dev.bin, gain: 30}}}\n"
            + _ZONE % "{method: fakepager, options: {channel: ops}}",
        )
    )
    # validate_options normalized the receiver's option in the loaded config.
    assert config.receivers["sdr"].options["device"] == str(tmp_path / "dev.bin")
    assert receivers_mod.receiver_sources()["fakesdr"] == PLUGIN_DIST
    assert alerters_mod.alerter_sources()["fakepager"] == PLUGIN_DIST


@pytest.mark.parametrize(
    ("receiver_options", "action", "message"),
    [
        (
            "{device: dev.bin, gain: 99}",
            "{channel: ops}",
            "receivers.sdr: gain must be 0-50",
        ),
        (
            "{device: nope.bin}",
            "{channel: ops}",
            "receivers.sdr.options.device: not found:",
        ),
        (
            "{device: dev.bin}",
            "{}",
            "zones.pad.rules.warn: fakepager needs options.channel",
        ),
        (
            "{device: dev.bin, gian: 3}",
            "{channel: ops}",
            "receivers.sdr.options: unknown option 'gian'; did you mean 'gain'? "
            "valid: device, gain",
        ),
        (
            "{device: dev.bin}",
            "{chanel: ops}",
            "zones.pad.rules.warn: fakepager options: unknown option 'chanel'; "
            "did you mean 'channel'?",
        ),
    ],
)
def test_third_party_option_problems(
    plugins, tmp_path, receiver_options, action, message
):
    _fake_plugins(plugins)
    (tmp_path / "dev.bin").write_text("")
    text = (
        _HOME
        + f"receivers: {{sdr: {{type: fakesdr, options: {receiver_options}}}}}\n"
        + _ZONE % f"{{method: fakepager, options: {action}}}"
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(_write(tmp_path, text))
    assert message in str(excinfo.value)


def test_a_crashing_validate_options_is_reported_not_raised(plugins, tmp_path):
    class Crashy(FakeSdrReceiver):
        @classmethod
        def validate_options(cls, options, *, config_dir):
            raise RuntimeError("plugin bug")

    plugins["pyaerial.receivers"].append(_EntryPoint("crashy", Crashy))
    with pytest.raises(
        ConfigError, match=r"receivers.c: options could not be validated"
    ):
        load_config(_write(tmp_path, _HOME + "receivers: {c: {type: crashy}}\n"))


# --- broken and misbehaving plugins ---------------------------------------------


def test_broken_plugin_is_skipped_once(plugins, tmp_path, caplog, capsys):
    plugins["pyaerial.receivers"].append(
        _EntryPoint("brokensdr", ImportError("No module named 'soapysdr'"))
    )
    with caplog.at_level(logging.ERROR, logger="pyaerial.plugins"):
        assert receivers_mod.available_receivers() == ["aoa", "dump1090", "readsb", "replay"]
        receivers_mod.available_receivers()  # discovery runs once
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "brokensdr" in errors[0].getMessage()

    # A config that does not use it still validates (and lists it as unavailable).
    good = _write(tmp_path, _HOME + "receivers: {main: {type: dump1090}}\n")
    cli.main(["validate", "-c", str(good)])
    out = capsys.readouterr().out
    assert "is valid" in out and "unavailable receiver brokensdr" in out

    # A config that does use it gets a clear error.
    bad = _write(tmp_path, _HOME + "receivers: {r: {type: brokensdr}}\n")
    with pytest.raises(ConfigError, match="type 'brokensdr' is not installed"):
        load_config(bad)


def test_plugin_without_known_options_is_refused(plugins, caplog):
    plugins["pyaerial.receivers"].append(_EntryPoint("lazy", NoOptionsReceiver))
    plugins["pyaerial.receivers"].append(_EntryPoint("notaclass", object()))
    with caplog.at_level(logging.ERROR, logger="pyaerial.plugins"):
        names = receivers_mod.available_receivers()
    assert "lazy" not in names and "notaclass" not in names
    unavailable = receivers_mod.unavailable_receivers()
    assert "known_options" in unavailable["lazy"]
    assert "not a Receiver subclass" in unavailable["notaclass"]


def test_builtin_names_win(plugins, caplog):
    plugins["pyaerial.receivers"].append(_EntryPoint("dump1090", FakeSdrReceiver))
    plugins["pyaerial.alerters"].append(_EntryPoint("webhook", FakePagerAlerter))
    with caplog.at_level(logging.WARNING, logger="pyaerial.plugins"):
        assert receivers_mod.receiver_class("dump1090") is Dump1090Receiver
        assert alerters_mod.alerter_class("webhook").__name__ == "WebhookAlerter"
    assert receivers_mod.receiver_sources()["dump1090"] == BUILTIN
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("a builtin receiver has that name" in w for w in warnings)
    assert any("a builtin alerter has that name" in w for w in warnings)


def test_builtins_register_without_package_metadata(plugins):
    # No entry points at all (an editable checkout without metadata).
    assert receivers_mod.receiver_sources() == {
        "aoa": BUILTIN,
        "dump1090": BUILTIN,
        "readsb": BUILTIN,
        "replay": BUILTIN,
    }
    assert alerters_mod.alerter_sources() == {"print": BUILTIN, "webhook": BUILTIN}


def test_unreadable_metadata_falls_back_to_builtins(plugins, monkeypatch):
    def _broken(*, group):
        raise OSError("corrupt metadata")

    monkeypatch.setattr(plugins_mod, "entry_points", _broken)
    assert receivers_mod.available_receivers() == ["aoa", "dump1090", "readsb", "replay"]


def test_own_entry_points_register_builtins(plugins):
    plugins["pyaerial.receivers"].append(
        _EntryPoint("dump1090", Dump1090Receiver, dist="pyaerial")
    )
    assert receivers_mod.receiver_sources()["dump1090"] == BUILTIN


def test_validate_lists_plugins_and_where_they_come_from(plugins, tmp_path, capsys):
    _fake_plugins(plugins)
    path = _write(tmp_path, _HOME + "receivers: {main: {type: dump1090}}\n")
    cli.main(["validate", "-c", str(path)])
    out = capsys.readouterr().out
    assert (
        "available receivers: aoa (builtin), dump1090 (builtin), "
        f"fakesdr ({PLUGIN_DIST}), readsb (builtin), replay (builtin)"
    ) in out
    assert f"available alerters: fakepager ({PLUGIN_DIST}), print (builtin)" in out


# --- the contract on the builtins -------------------------------------------------


def test_pyproject_declares_every_builtin():
    eps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "entry-points"
    ]
    for group, registry in (
        ("pyaerial.receivers", receivers_mod.RECEIVERS),
        ("pyaerial.alerters", alerters_mod.ALERTERS),
    ):
        assert eps[group] == registry._builtins


@pytest.mark.parametrize("name", ["aoa", "dump1090", "readsb", "replay"])
def test_builtin_receivers_declare_the_contract(name):
    cls = receivers_mod.receiver_class(name)
    assert isinstance(cls.known_options, frozenset) and cls.known_options
    assert callable(cls.validate_options)


@pytest.mark.parametrize("name", ["print", "webhook"])
def test_builtin_alerters_declare_the_contract(name):
    cls = alerters_mod.alerter_class(name)
    assert isinstance(cls.known_options, frozenset)
    assert cls.method == name


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, ("localhost", 30005, "beast")),
        ({"tcp_connection_port": 30002}, ("localhost", 30002, "avr")),
        ({"format": "raw"}, ("localhost", 30002, "avr")),
        (
            {"format": "binary", "tcp_connection_port": 31005},
            ("localhost", 31005, "beast"),
        ),
        ({"format": "sbs"}, ("localhost", 30002, "avr")),  # unknown means AVR
        (
            {"tcp_connection_ip": "feeder.lan", "tcp_connection_port": "30005"},
            ("feeder.lan", 30005, "beast"),
        ),
    ],
)
def test_dump1090_endpoint_is_the_one_format_rule(options, expected):
    assert Dump1090Receiver.endpoint(options) == ReceiverEndpoint(*expected)
    receiver = Dump1090Receiver("main", lambda *_a, **_k: None, dict(options))
    assert (receiver.port, receiver.format) == expected[1:]


def test_endpoint_rejects_a_non_numeric_port():
    with pytest.raises(ValueError):
        Dump1090Receiver.endpoint({"tcp_connection_port": "abc"})
