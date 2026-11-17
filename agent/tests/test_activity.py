import pytest

from pc_agent.activity import MAX_REPORT_AGE, Activity, ActivityHub, decode, encode, parse_report
from pc_agent.helper import describe


def test_report_roundtrip_and_limits():
    msg = decode(encode({"type": "report", "idle_seconds": 4.5, "app": "игра.exe",
                         "title": "x" * 1000}))
    act = parse_report(msg, received=1.0)
    assert act.app == "игра.exe" and act.idle_seconds == 4.5 and len(act.title) == 300


@pytest.mark.parametrize("bad", [b"[1]", b"not json", b'{"type": "other"}'])
def test_bad_messages_rejected(bad):
    with pytest.raises(ValueError):
        parse_report(decode(bad), 0.0)


def test_latest_expires():
    hub = ActivityHub()
    hub.push(Activity(0.0, "a.exe", "", received=100.0))
    assert hub.latest(100.0 + MAX_REPORT_AGE) is not None
    assert hub.latest(100.0 + MAX_REPORT_AGE + 0.1) is None


def test_tray_text():
    assert "нет связи" in describe(None)[0]
    text, color = describe({"known": True, "allowed": True, "remaining_minutes": 75,
                            "window": "08:00-21:00", "idle": False})
    assert text.startswith("Осталось 1 ч 15 мин") and color == "#2e7d32"
    assert describe({"known": True, "allowed": True, "remaining_minutes": 3})[1] == "#c62828"
    assert "закончилось" in describe({"known": True, "allowed": False,
                                      "reason": "Время за компьютером на сегодня закончилось."})[0]
