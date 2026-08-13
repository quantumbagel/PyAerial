"""
Centralized logging configuration for PyAerial.

All modules obtain loggers via :func:`logging.getLogger` under the ``pyaerial``
namespace; this module wires up a single console handler (and optionally a
rotating file handler) with a consistent format.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from pyaerial.constants import LOGGING_LEVELS

_LOG_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    level: str = "info",
    *,
    log_file: str | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    backups: int = 3,
) -> None:
    """
    Configure the root logger for the process.

    :param level: one of ``debug``/``info``/``warning``/``error``
    :param log_file: optional path for a rotating file handler
    :param max_bytes: rotation threshold for the file handler
    :param backups: number of rotated files to keep
    """
    numeric_level = LOGGING_LEVELS.get(level, logging.INFO)
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Remove handlers we may have added before (idempotent setup).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backups
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Quiet down noisy third-party libraries unless we are debugging.
    if numeric_level > logging.DEBUG:
        for noisy in ("pymongo", "kafka", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
