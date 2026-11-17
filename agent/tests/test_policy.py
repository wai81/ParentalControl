from datetime import date, datetime

import pytest
from pydantic import ValidationError

from pc_agent.policy import DayState, evaluate
from pc_shared.rules import DayRules, Rules, Window

MONDAY = date(2026, 11, 16)
SATURDAY = date(2026, 11, 21)


def rules(**kw) -> Rules:
    base = dict(
        child_user="kid",
        weekdays=DayRules(limit_minutes=60, window=Window(start="08:00", end="21:00")),
        weekends=DayRules(limit_minutes=120, window=Window(start="09:00", end="22:00")),
        warn_at_minutes=[10, 5, 1],
    )
    base.update(kw)
    return Rules(**base)


def at(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m)


def state(day: date = MONDAY, used_min: float = 0, **kw) -> DayState:
    return DayState(day=day, used_seconds=used_min * 60, **kw)


def test_allowed_inside_window_with_time_left():
    d = evaluate(rules(), state(used_min=20), at(MONDAY, "12:00"))
    assert d.allowed
    assert d.remaining_minutes == pytest.approx(40)


def test_weekend_uses_weekend_rules():
    d = evaluate(rules(), state(SATURDAY, used_min=90), at(SATURDAY, "12:00"))
    assert d.allowed
    assert d.limit_minutes == 120


def test_outside_window_is_denied():
    d = evaluate(rules(), state(), at(MONDAY, "07:59"))
    assert not d.allowed
    assert "08:00-21:00" in d.reason
    assert not evaluate(rules(), state(), at(MONDAY, "21:00")).allowed


def test_limit_exhausted():
    d = evaluate(rules(), state(used_min=60), at(MONDAY, "12:00"))
    assert not d.allowed
    assert "закончилось" in d.reason


def test_bonus_extends_limit():
    d = evaluate(rules(), state(used_min=60, bonus_minutes=30), at(MONDAY, "12:00"))
    assert d.allowed
    assert d.remaining_minutes == pytest.approx(30)


def test_blocked_has_priority():
    d = evaluate(rules(), state(blocked=True), at(MONDAY, "12:00"))
    assert not d.allowed
    assert "заблокировали" in d.reason


def test_remaining_is_limited_by_window_end():
    d = evaluate(rules(), state(), at(MONDAY, "20:45"))
    assert d.remaining_minutes == pytest.approx(15)


def test_warnings_fire_once_and_rearm_after_bonus():
    r = rules()
    d1 = evaluate(r, state(used_min=52), at(MONDAY, "12:00"))
    assert d1.warn_now == {10}
    d2 = evaluate(r, state(used_min=53, warned=d1.warned_after), at(MONDAY, "12:01"))
    assert d2.warn_now == frozenset()
    d3 = evaluate(r, state(used_min=56, warned=d2.warned_after), at(MONDAY, "12:04"))
    assert d3.warn_now == {5}
    # +30 bonus: remaining is 34 again, thresholds re-arm
    d4 = evaluate(r, state(used_min=56, bonus_minutes=30, warned=d3.warned_after),
                  at(MONDAY, "12:05"))
    assert d4.warned_after == frozenset() and d4.warn_now == frozenset()


def test_window_until_midnight():
    r = rules(weekdays=DayRules(limit_minutes=600, window=Window(start="00:00", end="24:00")))
    assert evaluate(r, state(), at(MONDAY, "23:59")).allowed


@pytest.mark.parametrize("bad", ["25:00", "8", "10:60", "24:30"])
def test_bad_time_rejected(bad):
    with pytest.raises(ValidationError):
        Window(start="08:00", end=bad)


def test_window_start_must_precede_end():
    with pytest.raises(ValidationError):
        Window(start="21:00", end="08:00")


def test_rules_normalization():
    r = Rules(child_user=" kid ", warn_at_minutes=[1, 10, 5, 10],
              blocked_apps=["Roblox.EXE", " minecraft* ", "roblox"])
    assert r.child_user == "kid"
    assert r.warn_at_minutes == [10, 5, 1]
    assert r.blocked_apps == ["roblox", "minecraft*"]
    assert Window(start="8:05", end="9:00").start == "08:05"
