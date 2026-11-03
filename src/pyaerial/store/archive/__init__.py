"""SQLite research archive: flights, labelled telemetry, integrity verdicts,
alert episodes, and (optionally) every raw receiver frame.

- :class:`~pyaerial.store.archive.writer.ArchiveWriter`: the engine and
  ``pyaerial reset``; creates, upgrades and writes the file. Import it from
  its module: the package does not export it, so importing the package
  (as the portal does) never loads the write paths.
- :class:`ArchiveReader`: the portal; opens the file read-only and never
  runs DDL.
- :mod:`~pyaerial.store.archive.schema`: DDL, version detection, migrations.
"""

from pyaerial.store.archive._conn import HistoryUnavailable, LeaseLost, RowRejected
from pyaerial.store.archive.reader import ArchiveReader
from pyaerial.store.archive.schema import SCHEMA_VERSION, HistorySchemaMismatch

__all__ = [
    "SCHEMA_VERSION",
    "ArchiveReader",
    "HistorySchemaMismatch",
    "HistoryUnavailable",
    "LeaseLost",
    "RowRejected",
]
