from __future__ import annotations

from pathlib import Path

from pyaerial.constants import DEFAULT_AIRCRAFT_DB, WHEN_FIELDS


def test_default_aircraft_db_is_project_root():
    path = Path(DEFAULT_AIRCRAFT_DB)
    assert path.name == "aircraft.db"
    # Must resolve inside the repo, not the parent of the project.
    assert path.parent.name == "PyAerial"


def test_when_fields_include_readme_aliases():
    assert "horizontal_speed" in WHEN_FIELDS
    assert "direction" in WHEN_FIELDS
    assert "speed" in WHEN_FIELDS
    assert "heading" in WHEN_FIELDS
