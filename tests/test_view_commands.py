from __future__ import annotations

import json

from pyaerial.view.commands import cmd_dump
from pyaerial.view.live_display import format_dump1090_table, live_empty_message


class _FakeAircraftDB:
    def lookup_cached(self, icao: str):
        return {"icao": icao.lower(), "model": "A320"}


def test_dump_aircraft_prints_cached_record(capsys):
    cmd_dump(None, ["dump", "aircraft", "ABC123"], _FakeAircraftDB())
    out = capsys.readouterr().out
    assert json.loads(out) == {"icao": "abc123", "model": "A320"}


def test_dump_opensky_alias_still_works(capsys):
    cmd_dump(None, ["dump", "opensky", "ABC123"], _FakeAircraftDB())
    out = capsys.readouterr().out
    assert json.loads(out)["model"] == "A320"


def test_dump_aircraft_requires_icao(capsys):
    cmd_dump(None, ["dump", "aircraft"], _FakeAircraftDB())
    err = capsys.readouterr().out
    assert "requires an ICAO" in err


def test_live_empty_message_distinguishes_causes():
    assert "Redis" in live_empty_message(redis_ok=False)
    assert "pyaerial run" in live_empty_message(redis_ok=True, engine_seen_at=None)
    assert live_empty_message(redis_ok=True, engine_seen_at=1_700_000_000, now=1_700_000_001) == (
        "No aircraft on the live feed."
    )


def test_format_dump1090_table_uses_empty_reason():
    table = format_dump1090_table([], redis_ok=True, engine_seen_at=None)
    assert "Tracking engine is not running" in table

