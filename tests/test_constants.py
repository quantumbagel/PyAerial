from __future__ import annotations

from pathlib import Path

from pyaerial.constants import DEFAULT_AIRCRAFT_DB, WHEN_FIELDS


def test_default_aircraft_db_is_aircraft_db_filename():
    path = Path(DEFAULT_AIRCRAFT_DB)
    assert path.name == "aircraft.db"


def test_when_fields_include_readme_aliases():
    assert "horizontal_speed" in WHEN_FIELDS
    assert "direction" in WHEN_FIELDS
    assert "speed" in WHEN_FIELDS
    assert "heading" in WHEN_FIELDS
