from __future__ import annotations

import json

from pyaerial.view.commands import cmd_dump


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
