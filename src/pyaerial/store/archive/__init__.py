"""SQLite research archive: flights, labelled telemetry, integrity verdicts,
alert episodes, and (optionally) every raw receiver frame.

- :class:`ArchiveWriter`: the engine and ``pyaerial reset``; creates,
  upgrades and writes the file.
- :class:`ArchiveReader`: the portal; opens the file read-only and never
  runs DDL.
- :mod:`~pyaerial.store.archive.schema`: DDL, version detection, migrations.
"""

from pyaerial.store.archive._conn import HistoryUnavailable, LeaseLost, RowRejected
from pyaerial.store.archive.reader import ArchiveReader
from pyaerial.store.archive.schema import SCHEMA_VERSION, HistorySchemaMismatch
from pyaerial.store.archive.writer import ArchiveWriter

__all__ = [
    "SCHEMA_VERSION",
    "ArchiveReader",
    "ArchiveWriter",
    "HistorySchemaMismatch",
    "HistoryUnavailable",
    "LeaseLost",
    "RowRejected",
]
