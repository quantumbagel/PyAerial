from __future__ import annotations

import pytest

from pyaerial import cli
from pyaerial.cli import _build_parser


def test_cli_rejects_mock_flag():
    parser = _build_parser()
    for command in ("run", "web", "reset"):
        with pytest.raises(SystemExit):
            parser.parse_args([command, "--mock"])


def test_web_cli_has_host_and_port():
    parser = _build_parser()
    args = parser.parse_args(["web", "--host", "0.0.0.0", "-p", "8080"])
    assert args.command == "web"
    assert args.host == "0.0.0.0"
    assert args.port == 8080
    assert not hasattr(args, "mock")


def test_reset_has_no_mock_flag():
    parser = _build_parser()
    reset = parser.parse_args(["reset", "--yes"])
    assert not hasattr(reset, "mock")
    assert reset.yes is True
    assert reset.icao is None


def test_reset_accepts_icao():
    parser = _build_parser()
    args = parser.parse_args(["reset", "abc123", "-y"])
    assert args.command == "reset"
    assert args.icao == "abc123"
    assert args.yes is True
    assert args.force is False
    assert parser.parse_args(["reset", "--yes", "--force"]).force is True


def test_view_and_live_subcommands_are_gone():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["view"])
    with pytest.raises(SystemExit):
        parser.parse_args(["live"])


# --- Failures exit with a message, not a traceback ---------------------------


_LOG_FILE_YAML = """
home: {latitude: 35.7, longitude: -78.6}
receivers: {main: {type: dump1090}}
logging: {file: %s}
"""


def test_web_port_must_be_a_tcp_port(capsys):
    parser = _build_parser()
    for bad in ("70000", "0", "http"):
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["web", "-p", bad])
        assert excinfo.value.code == 2
    assert "not a TCP port" in capsys.readouterr().err
    assert parser.parse_args(["web", "-p", "65535"]).port == 65535


@pytest.mark.parametrize("command", ["web", "run"])
def test_unopenable_log_file_is_a_config_error(tmp_path, capsys, monkeypatch, command):
    path = tmp_path / "config.yaml"
    path.write_text(_LOG_FILE_YAML % (tmp_path / "missing" / "pyaerial.log"))

    def _never(*_args, **_kwargs):
        raise AssertionError("service must not start")

    monkeypatch.setattr(cli, "run_engine", _never)
    monkeypatch.setattr("pyaerial.webapp.run_webapp", _never)
    with pytest.raises(SystemExit) as excinfo:
        cli.main([command, "-c", str(path)])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "logging.file" in err and "pyaerial.log" in err


def test_version_flag(capsys):
    from pyaerial import __version__

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"pyaerial {__version__}"


def test_no_command_prints_usage_and_exits_2(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2
    assert "usage: pyaerial" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["zzz", "abc12", "abc1234", "abcxyz", "0x1234"])
def test_reset_rejects_invalid_icao(capsys, bad):
    with pytest.raises(SystemExit) as excinfo:
        _build_parser().parse_args(["reset", "--yes", bad])
    assert excinfo.value.code == 2
    assert "6 hex digits" in capsys.readouterr().err


def test_reset_normalizes_icao():
    assert _build_parser().parse_args(["reset", "--yes", "ABC123"]).icao == "abc123"


def test_aircraft_db_help_shows_a_relative_default(capsys):
    import os

    for command in ("run", "web"):
        with pytest.raises(SystemExit):
            _build_parser().parse_args([command, "--help"])
        out = capsys.readouterr().out
        assert "aircraft.db" in out
        assert os.getcwd() not in out.replace("\n", "").replace(" ", "") or os.getcwd() == "/"


def test_web_startup_goes_through_logging(monkeypatch, caplog):
    import logging

    from helpers import make_config
    from pyaerial import webapp

    class _Closable:
        def close(self) -> None:
            pass

    config = make_config()
    monkeypatch.setattr(
        webapp, "_open_stores", lambda _path, cfg: (cfg, _Closable(), _Closable())
    )
    monkeypatch.setattr(webapp, "create_app", lambda **_kw: object())
    calls = {}
    monkeypatch.setattr(webapp.uvicorn, "run", lambda app, **kw: calls.update(kw))
    with caplog.at_level(logging.INFO, logger="pyaerial.webapp"):
        webapp.run_webapp(config=config, aircraft_db_path="", host="127.0.0.1", port=1)
    assert "Starting PyAerial web portal on http://127.0.0.1:1" in caplog.text
    assert calls["log_config"] is None  # uvicorn logs through our handlers too
    assert calls["log_level"] == "info"


# --- pyaerial validate -------------------------------------------------------
# validate rejects everything the engine would reject, readably.


_HOME_YAML = "home: {latitude: 35.7275, longitude: -78.6959}\n"


_RECEIVER_YAML = "receivers: {main: {type: dump1090}}\n"


_SQUARE = "[[35.72, -78.70], [35.73, -78.70], [35.73, -78.69], [35.72, -78.69]]"


def _zone(
    coordinates: str = _SQUARE, *, rules: str | None = None, file: str = ""
) -> str:
    where = f"file: {file}" if file else f"coordinates: {coordinates}"
    rules = rules or "[{name: warn, when: {eta: {max: 60}}, dwell_seconds: 1}]"
    return f"zones:\n  pad:\n    {where}\n    rules: {rules}\n"


def _webhook(options: str) -> str:
    return _zone(
        rules="[{name: warn, when: {eta: {max: 60}}, dwell_seconds: 1, "
        f"on_activate: [{{method: webhook, options: {{{options}}}}}]}}]"
    )


# (case id, config text, files to create {name: content}, env, expected message)
CASES = [
    ("yaml-syntax", "home: [unclosed\n", {}, {}, "could not parse"),
    ("not-a-mapping", "- just\n- a list\n", {}, {}, "mapping at the top level"),
    ("missing-home", _RECEIVER_YAML, {}, {}, "home: Field required"),
    ("unknown-key", _HOME_YAML + _RECEIVER_YAML + "tracking: {hertz: 2}\n", {}, {}, "hertz"),
    ("bad-hz", _HOME_YAML + _RECEIVER_YAML + "tracking: {hz: 0}\n", {}, {}, "tracking.hz"),
    (
        "unknown-receiver",
        _HOME_YAML + "receivers: {x: {type: sdrplay}}\n",
        {},
        {},
        "unknown type",
    ),
    (
        "replay-missing-file",
        _HOME_YAML + "receivers: {r: {type: replay, options: {path: nope.raw}}}\n",
        {},
        {},
        "not found",
    ),
    (
        "replay-bad-speed",
        _HOME_YAML + "receivers: {r: {type: replay, options: {path: c.raw, speed: 0}}}\n",
        {"c.raw": ""},
        {},
        "receivers.r: replay options.speed must be greater than 0",
    ),
    (
        "aoa-no-port",
        _HOME_YAML + "receivers: {a: {type: aoa, host: localhost}}\n",
        {},
        {},
        "receivers.a: aoa receiver requires a port",
    ),
    (
        "dump1090-options-format",
        _HOME_YAML + "receivers: {d: {type: dump1090, options: {format: sbs}}}\n",
        {},
        {},
        "receivers.d: format 'sbs'",
    ),
    (
        "dump1090-format-port-mismatch",
        _HOME_YAML + "receivers: {d: {type: dump1090, port: 30005, format: avr}}\n",
        {},
        {},
        "AVR text",
    ),
    (
        "bowtie",
        _HOME_YAML
        + _RECEIVER_YAML
        + _zone("[[35.72, -78.70], [35.73, -78.69], [35.72, -78.69], [35.73, -78.70]]"),
        {},
        {},
        "zones.pad.coordinates",
    ),
    (
        "two-point-zone",
        _HOME_YAML
        + _RECEIVER_YAML
        + _zone("[[35.72, -78.70], [35.73, -78.70], [35.72, -78.70]]"),
        {},
        {},
        "3 distinct",
    ),
    (
        "duplicate-rule",
        _HOME_YAML
        + _RECEIVER_YAML
        + _zone(
            rules="[{name: w, when: {eta: {max: 60}}, dwell_seconds: 1}, "
            "{name: w, when: {eta: {max: 9}}, dwell_seconds: 1}]"
        ),
        {},
        {},
        "duplicate rule name",
    ),
    (
        "zone-file-missing",
        _HOME_YAML + _RECEIVER_YAML + _zone(file="z.geojson"),
        {},
        {},
        "not found",
    ),
    (
        "zone-file-garbage",
        _HOME_YAML + _RECEIVER_YAML + _zone(file="z.geojson"),
        {"z.geojson": "{not json"},
        {},
        "zones.pad.file",
    ),
    (
        "unknown-alerter",
        _HOME_YAML
        + _RECEIVER_YAML
        + _zone(
            rules="[{name: w, when: {eta: {max: 60}}, dwell_seconds: 1, "
            "on_activate: [{method: discord}]}]"
        ),
        {},
        {},
        "unknown alerter",
    ),
    (
        "webhook-format",
        _HOME_YAML + _RECEIVER_YAML + _webhook("url: 'https://h.example/x', format: xml"),
        {},
        {},
        "webhook format",
    ),
    (
        "webhook-http",
        _HOME_YAML + _RECEIVER_YAML + _webhook("url: 'http://h.example/SECRET'"),
        {},
        {},
        "must be https",
    ),
    (
        "unset-env-var",
        _HOME_YAML + "receivers: {main: {type: dump1090, host: '${R5_UNSET_VAR}'}}\n",
        {},
        {},
        "R5_UNSET_VAR",
    ),
    (
        "bad-env-beast-port",
        _HOME_YAML + _RECEIVER_YAML,
        {},
        {"PYAERIAL_BEAST_PORT": "x"},
        "PYAERIAL_BEAST_PORT",
    ),
    (
        "redis-password-file-missing",
        _HOME_YAML + _RECEIVER_YAML,
        {},
        {"PYAERIAL_REDIS_PASSWORD_FILE": "/nonexistent/redis_password"},
        "redis_password_file",
    ),
    (
        "bad-redis-uri",
        _HOME_YAML + _RECEIVER_YAML,
        {},
        {"PYAERIAL_REDIS": "localhost:6379"},
        "redis_uri",
    ),
]


@pytest.mark.parametrize(
    ("text", "files", "env", "message"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_validate_rejects_with_a_readable_message(
    tmp_path, monkeypatch, capsys, text, files, env, message
):
    monkeypatch.delenv("R5_UNSET_VAR", raising=False)
    for name in (
        "PYAERIAL_BEAST_PORT",
        "PYAERIAL_REDIS",
        "PYAERIAL_REDIS_PASSWORD_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    path = tmp_path / "config.yaml"
    path.write_text(text)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["validate", "-c", str(path)])
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("INVALID:")
    assert message in captured.err
    assert "Traceback" not in captured.err + captured.out
    assert "SECRET" not in captured.err


def test_validate_accepts_a_good_config(tmp_path, monkeypatch, capsys):
    for name in (
        "PYAERIAL_BEAST_PORT",
        "PYAERIAL_REDIS",
        "PYAERIAL_REDIS_PASSWORD_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "c.raw").write_text("")
    path = tmp_path / "config.yaml"
    path.write_text(
        _HOME_YAML
        + "receivers:\n"
        + "  main: {type: dump1090, port: 30005}\n"
        + "  arr: {type: aoa, port: 30010}\n"
        + "  rec: {type: replay, options: {path: c.raw, speed: 2}}\n"
        + _webhook("url: 'https://h.example/x', format: discord")
    )
    cli.main(["validate", "-c", str(path)])
    out = capsys.readouterr().out
    assert "is valid" in out and "main, arr, rec" in out
