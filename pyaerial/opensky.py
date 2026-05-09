"""
OpenSky / static aircraft database lookups (CSV).
"""
from __future__ import annotations

import csv
import io
import os
from pathlib import Path

from pyaerial.constants import STORE_OPENSKY_HEADER

ENV_OPENSKY_CSV = "PYAERIAL_OPENSKY_CSV"


def resolve_opensky_csv_path() -> Path:
    env = os.environ.get(ENV_OPENSKY_CSV)
    if env:
        return Path(env).expanduser().resolve()
    return (Path.cwd() / "database.csv").resolve()


def load_airplane_info(icao: str, csv_path: Path | None = None) -> dict | None:
    """
    Look up aircraft metadata by ICAO hex in a CSV export (e.g. OpenSky-style).
    """
    path = csv_path or resolve_opensky_csv_path()
    if not path.is_file():
        return None
    prefix = icao.upper()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.upper().startswith(prefix):
                plane = list(csv.reader(io.StringIO(line), quotechar="'"))[0]
                data = {}
                for ind, thing in enumerate(plane):
                    if ind < len(STORE_OPENSKY_HEADER):
                        data[STORE_OPENSKY_HEADER[ind]] = thing
                return data
    return None
