from __future__ import annotations

import json
import time

from pyaerial.view.commands import cmd_dump, cmd_reset
from pyaerial.view.live_display import format_dump1090_table, live_empty_message


class _FakeAircraftDB:
    def lookup_cached_fast(self, icao: str):
        return {"icao": icao.lower(), "model": "A320"}

    def lookup_cached(self, icao: str):
        raise AssertionError("view dump must not hit the network lookup")


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


def test_status_reports_redis_down(capsys):
    from pyaerial.view.commands import cmd_status

    class _Live:
        def ping(self):
            return False

        def get_flights(self):
            raise AssertionError("must not treat Redis-down as zero flights")

    cmd_status(None, live_store=_Live())
    out = capsys.readouterr().out
    assert "Redis" in out


def test_reset_reports_history_disconnect(capsys):
    class _History:
        def reset_all(self):
            return False

    cmd_reset(_History(), ["reset"], last_reset=True)
    out = capsys.readouterr().out
    assert "disconnected" in out.lower()


def test_reset_skips_live_store_while_engine_is_running(capsys):
    class _Live:
        def engine_seen_at(self):
            return time.time()

        def clear_all(self):
            raise AssertionError("must not clear live Redis while engine is up")

    cmd_reset(None, ["reset"], last_reset=True, live_store=_Live())
    out = capsys.readouterr().out
    assert "engine is running" in out.lower()


def test_format_dump1090_table_uses_empty_reason():
    table = format_dump1090_table([], redis_ok=True, engine_seen_at=None)
    assert "Tracking engine is not running" in table

