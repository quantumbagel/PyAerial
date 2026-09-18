from __future__ import annotations

import pytest

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


def test_view_and_live_subcommands_are_gone():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["view"])
    with pytest.raises(SystemExit):
        parser.parse_args(["live"])
