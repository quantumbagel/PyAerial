from __future__ import annotations

import logging

import pytest

from pyaerial.logging_setup import setup_logging


def test_repeated_setup_closes_previous_file_handler(tmp_path):
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        setup_logging("info", log_file=str(tmp_path / "a.log"))
        first = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
        assert len(first) == 1
        assert first[0].stream is not None
        setup_logging("info", log_file=str(tmp_path / "b.log"))
        assert first[0] not in root.handlers
        assert first[0].stream is None  # closed, file descriptor released
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


# --- HTTP client loggers never log request paths -----------------------------
# Webhook URLs carry secrets.


@pytest.fixture
def restore_root():
    root = logging.getLogger()
    saved = list(root.handlers), root.level
    names = ("urllib3", "requests")
    levels = {name: logging.getLogger(name).level for name in names}
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved[0]:
        root.addHandler(handler)
    root.setLevel(saved[1])
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.mark.parametrize("level", ["debug", "info", "warning", "error"])
def test_http_client_loggers_stay_above_debug(restore_root, level):
    setup_logging(level)
    for name in ("urllib3", "urllib3.connectionpool", "requests"):
        logger = logging.getLogger(name)
        assert logger.getEffectiveLevel() >= logging.INFO, (level, name)
        # urllib3 logs '"POST /api/webhooks/<id>/<token> HTTP/1.1" 204' at DEBUG.
        assert not logger.isEnabledFor(logging.DEBUG), (level, name)
    # Our own debug logging still works.
    assert logging.getLogger("pyaerial.x").isEnabledFor(logging.DEBUG) == (level == "debug")
