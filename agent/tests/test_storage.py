from datetime import date

from pc_agent.storage import Store
from pc_shared.events import Event, EventKind

D1 = date(2026, 11, 16)
D2 = date(2026, 11, 17)


def test_days_are_independent(tmp_path):
    with Store(tmp_path / "a.db") as s:
        s.add_usage(D1, 120)
        s.add_usage(D1, 30)
        s.add_bonus(D1, 15)
        s.set_blocked(D1, True)
        s.set_warned(D1, frozenset({5, 10}))
        s.add_usage(D2, 60)
        d1, d2 = s.load_day(D1), s.load_day(D2)
    assert (d1.used_seconds, d1.bonus_minutes, d1.blocked, d1.warned) == (150, 15, True, {5, 10})
    assert (d2.used_seconds, d2.bonus_minutes, d2.blocked) == (60, 0, False)


def test_two_connections_see_each_other(tmp_path):
    with Store(tmp_path / "a.db") as agent, Store(tmp_path / "a.db") as cli:
        agent.add_usage(D1, 60)
        cli.add_bonus(D1, 30)
        agent.add_usage(D1, 60)
        state = agent.load_day(D1)
    assert state.used_seconds == 120 and state.bonus_minutes == 30


def test_events_roundtrip(tmp_path):
    with Store(tmp_path / "a.db") as s:
        s.add_event(Event(kind=EventKind.PROCESS_STARTED, data={"name": "игра.exe"}))
        s.add_event(Event(kind=EventKind.WARNING))
        events = s.recent_events(10)
    assert [e.kind for e in events] == [EventKind.PROCESS_STARTED, EventKind.WARNING]
    assert events[0].data["name"] == "игра.exe"


def test_purge_keeps_recent_data(tmp_path):
    from datetime import datetime

    with Store(tmp_path / "a.db") as s:
        old_ts = datetime(2026, 1, 1, 12, 0).astimezone()
        s.add_event(Event(ts=old_ts, kind=EventKind.WARNING))
        s.add_event(Event(kind=EventKind.WARNING))
        s.add_usage(date(2025, 1, 1), 60)
        s.add_app_usage(date(2025, 1, 1), "game.exe", 60)
        s.add_usage(D1, 60)
        deleted = s.purge(D2, events_days=90, usage_days=400)
        assert deleted == 1 and len(s.recent_events(10)) == 1
        assert [d for d, _ in s.history(10)] == [D1]
        assert s.app_usage(date(2025, 1, 1)) == []
