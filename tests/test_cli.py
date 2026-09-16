from __future__ import annotations

import pytest

from pyaerial.cli import _build_parser


def test_web_cli_rejects_mock_flag():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["web", "--mock"])


def test_web_cli_has_host_and_port():
    parser = _build_parser()
    args = parser.parse_args(["web", "--host", "0.0.0.0", "-p", "8080"])
    assert args.command == "web"
    assert args.host == "0.0.0.0"
    assert args.port == 8080
    assert not hasattr(args, "mock")


def test_live_and_view_keep_mock_flag():
    parser = _build_parser()
    live = parser.parse_args(["live", "--mock"])
    view = parser.parse_args(["view", "--mock"])
    assert live.mock is True
    assert view.mock is True
