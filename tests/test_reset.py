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
            raise AssertionError("must not delete history while engine is up")

    class _Live:
        def engine_seen_at(self):
            return time.time()

        def clear_all(self):
            raise AssertionError("must not clear live Redis while engine is up")

    code = reset_history(_History(), live_store=_Live(), yes=True)
    err = capsys.readouterr().err
    assert code == 1
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
            raise AssertionError("must not delete history for a live icao")

    class _Live:
        def engine_seen_at(self):
            return time.time()

        def get_flights(self):
            return [{"flight_id": "abc123-1", "icao": "abc123"}]

        def pop_flight(self, flight_id):
            raise AssertionError("must not drop live tracks while engine is up")

    code = reset_history(_History(), live_store=_Live(), icao="ABC123", yes=True)
    err = capsys.readouterr().err
    assert code == 1
    assert "engine is running" in err.lower()


def test_reset_icao_history_when_engine_running_but_icao_not_live(capsys):
    class _History:
        deleted: list[str] = []

        def delete_icao(self, icao):
            self.deleted.append(icao)
            return True

    class _Live:
        def engine_seen_at(self):
            return time.time()

        def get_flights(self):
            return [{"flight_id": "ffffee-1", "icao": "ffffee"}]

        def pop_flight(self, flight_id):
            return None

    history = _History()
    history.deleted = []
    code = reset_history(history, live_store=_Live(), icao="ABC123", yes=True)
    assert code == 0
    assert history.deleted == ["abc123"]


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


class _UnreachableLive:
    """Redis down: no heartbeat can be read, which is not proof of 'stopped'."""

    def ping(self):
        return False

    def engine_seen_at(self):
        return None

    def get_flights(self):
        raise AssertionError("must not read live flights while unreachable")

    def clear_all(self):
        raise AssertionError("must not clear an unreachable live store")


def test_reset_refuses_when_engine_state_unknown(capsys):
    class _History:
        def reset_all(self):
            raise AssertionError("must not wipe history when the engine may be running")

        def delete_icao(self, icao):
            raise AssertionError("must not delete history when the engine may be running")

    for icao in (None, "abc123"):
        code = reset_history(_History(), live_store=_UnreachableLive(), icao=icao, yes=True)
        err = capsys.readouterr().err
        assert code == 1
        assert "--force" in err


def test_reset_force_proceeds_when_engine_state_unknown(capsys):
    class _History:
        calls: list[str] = []

        def reset_all(self):
            self.calls.append("all")
            return True

        def delete_icao(self, icao):
            self.calls.append(icao)
            return True

    history = _History()
    history.calls = []
    assert reset_history(history, live_store=_UnreachableLive(), yes=True, force=True) == 0
    assert "not cleared" in capsys.readouterr().out
    assert (
        reset_history(
            history, live_store=_UnreachableLive(), icao="ABC123", yes=True, force=True
        )
        == 0
    )
    assert history.calls == ["all", "abc123"]


def test_force_never_overrides_a_running_engine(capsys):
    class _History:
        def reset_all(self):
            raise AssertionError("engine is confirmed running")

    class _Live:
        def ping(self):
            return True

        def engine_seen_at(self):
            return time.time()

    assert reset_history(_History(), live_store=_Live(), yes=True, force=True) == 1


def test_reset_reports_locked_database_cleanly(capsys):
    from pyaerial.store.history import HistoryUnavailable

    class _History:
        def reset_all(self):
            raise HistoryUnavailable("history archive is unavailable (reset): database is locked")

    code = reset_history(_History(), yes=True)
    assert code == 1
    assert "database is locked" in capsys.readouterr().err


def test_reset_without_yes_refuses_non_tty(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code = reset_history(None, yes=False)
    err = capsys.readouterr().err
    assert code == 1
    assert "--yes" in err
