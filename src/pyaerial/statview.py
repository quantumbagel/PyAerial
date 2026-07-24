"""
Backward compatibility module for statview.

Delegates functionality to ``pyaerial.view``.
"""
from __future__ import annotations

from pyaerial.view import (
    run_statview,
    run_view,
    _cmd_status,
    _cmd_list,
    _cmd_reset,
    _cmd_dump,
    _dump_plane,
    _dump_flight,
    _verify_plane,
    _verify_flight,
    HELP_TEXT,
)

__all__ = [
    "run_statview",
    "run_view",
    "_cmd_status",
    "_cmd_list",
    "_cmd_reset",
    "_cmd_dump",
    "_dump_plane",
    "_dump_flight",
    "_verify_plane",
    "_verify_flight",
    "HELP_TEXT",
]
