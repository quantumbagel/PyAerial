from __future__ import annotations

import time

from pyaerial.view.reset import reset_history


def test_reset_reports_history_disconnect(capsys):
    class _History:
        def reset_all(self):
            return False

    code = reset_history(_History(), yes=True)
    err = capsys.readouterr().err
    assert code == 1
    assert "disconnected" in err.lower()


def test_reset_skips_live_store_while_engine_is_running(capsys):
    class _History:
        def reset_all(self):
            return True

    class _Live:
        def engine_seen_at(self):
            return time.time()

        def clear_all(self):
            raise AssertionError("must not clear live Redis while engine is up")

    code = reset_history(_History(), live_store=_Live(), yes=True)
    err = capsys.readouterr().err
    assert code == 0
    assert "engine is running" in err.lower()


def test_reset_clears_live_when_engine_is_stopped(capsys):
    class _History:
        def reset_all(self):
            return True

    class _Live:
        cleared = False

        def engine_seen_at(self):
            return None

        def clear_all(self):
            self.cleared = True

    live = _Live()
    code = reset_history(_History(), live_store=live, yes=True)
    out = capsys.readouterr().out
    assert code == 0
    assert live.cleared
    assert "Dropped all planes" in out


def test_reset_icao_skips_live_while_engine_is_running(capsys):
    class _History:
        def delete_icao(self, icao):
            assert icao == "abc123"
            return True

    class _Live:
        def engine_seen_at(self):
            return time.time()

        def get_flights(self):
            raise AssertionError("must not drop live tracks while engine is up")

    code = reset_history(_History(), live_store=_Live(), icao="ABC123", yes=True)
    err = capsys.readouterr().err
    assert code == 0
    assert "engine is running" in err.lower()


def test_reset_icao_pops_live_when_engine_is_stopped(capsys):
    class _History:
        def delete_icao(self, icao):
            assert icao == "abc123"
            return True

    class _Live:
        popped: list[str] = []

        def engine_seen_at(self):
            return None

        def get_flights(self):
            return [{"flight_id": "abc123-1", "icao": "abc123"}]

        def pop_flight(self, flight_id):
            self.popped.append(flight_id)

    live = _Live()
    live.popped = []
    code = reset_history(_History(), live_store=live, icao="ABC123", yes=True)
    out = capsys.readouterr().out
    assert code == 0
    assert live.popped == ["abc123-1"]
    assert "Dropped plane" in out


def test_reset_without_yes_refuses_non_tty(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code = reset_history(None, yes=False)
    err = capsys.readouterr().err
    assert code == 1
    assert "--yes" in err
