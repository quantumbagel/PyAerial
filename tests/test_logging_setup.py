from __future__ import annotations

import logging

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
