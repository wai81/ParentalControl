from datetime import date, datetime

import pytest

from pc_agent.control import ControlHandler
from pc_agent.dialog import ParentPanel, format_minutes
from pc_agent.rules_file import save_rules
from pc_agent.secret import AttemptLimiter, hash_password, save_password, verify_password
from pc_agent.storage import Store
from pc_shared.events import EventKind
from pc_shared.rules import DayRules, Rules, Window

NOON = datetime(2026, 11, 16, 12, 0)
MONDAY = date(2026, 11, 16)
PASSWORD = "secret-123"


def test_hash_and_verify():
    record = hash_password(PASSWORD)
    assert PASSWORD not in str(record)
    assert verify_password(PASSWORD, record)
    assert not verify_password("secret-124", record)
    assert hash_password(PASSWORD)["salt"] != record["salt"]


def test_short_password_rejected(tmp_path):
    with pytest.raises(ValueError):
        save_password(tmp_path, "12345", restrict=False)


def test_limiter_locks_after_failures():
    limiter = AttemptLimiter(max_failures=3, lock_seconds=60)
    for _ in range(2):
        limiter.record(False, now=0)
    limiter.record(True, now=0)  # success resets the counter
    for _ in range(3):
        limiter.record(False, now=1)
    assert limiter.locked_for(now=30) == pytest.approx(31)
    assert limiter.locked_for(now=61) == 0


@pytest.fixture
def handler(tmp_path):
    save_rules(tmp_path / "rules.json", Rules(
        child_user="kid",
        weekdays=DayRules(limit_minutes=60, window=Window(start="08:00", end="21:00")),
    ))
    return ControlHandler(tmp_path, AttemptLimiter(max_failures=3, lock_seconds=300)), tmp_path


def send(h, mono=0.0, **message):
    return h.handle(message, now=NOON, mono=mono)


def test_status_needs_no_password(handler):
    h, _ = handler
    reply = send(h, action="status")
    assert reply["ok"] and reply["remaining_minutes"] == 60 and reply["password_set"] is False


def test_commands_refused_without_password_set(handler):
    h, _ = handler
    reply = send(h, action="bonus", minutes=30, password="anything")
    assert reply["error"] == "no_password"


def test_bonus_block_unblock_with_password(handler):
    h, data = handler
    save_password(data, PASSWORD, restrict=False)
    reply = send(h, action="bonus", minutes=30, password=PASSWORD)
    assert reply["ok"] and reply["remaining_minutes"] == 90
    assert send(h, action="block", password=PASSWORD)["blocked"] is True
    assert send(h, action="unblock", password=PASSWORD)["blocked"] is False
    with Store(data / "agent.db") as store:
        state = store.load_day(MONDAY)
        events = [e for e in store.recent_events(50) if e.kind == EventKind.PARENT_COMMAND]
    assert state.bonus_minutes == 30 and not state.blocked
    assert [e.data["action"] for e in events] == ["bonus", "block", "unblock"]


@pytest.mark.parametrize("minutes", [0, 601, "abc", None])
def test_bad_bonus_rejected(handler, minutes):
    h, data = handler
    save_password(data, PASSWORD, restrict=False)
    assert send(h, action="bonus", minutes=minutes, password=PASSWORD)["error"] == "bad_request"


def test_wrong_password_logged_and_locks(handler):
    h, data = handler
    save_password(data, PASSWORD, restrict=False)
    for _ in range(3):
        assert send(h, action="block", password="guess")["error"] == "wrong_password"
    # Locked now: even the right password is refused, nothing is changed.
    reply = send(h, mono=10, action="block", password=PASSWORD)
    assert reply["error"] == "locked" and "5 мин" in reply["message"]
    assert send(h, mono=301, action="block", password=PASSWORD)["ok"]
    with Store(data / "agent.db") as store:
        kinds = [e.kind for e in store.recent_events(50)]
    assert kinds.count(EventKind.WRONG_PASSWORD) == 3


def test_unknown_action(handler):
    h, _ = handler
    assert send(h, action="format_c")["error"] == "bad_request"


# ---------- parent window logic ----------


class FakeService:
    def __init__(self, handler):
        self.handler = handler
        self.sent = []

    def __call__(self, message):
        self.sent.append(message)
        return self.handler.handle(message, now=NOON, mono=0.0)


def test_panel_remembers_password_after_success(handler):
    h, data = handler
    save_password(data, PASSWORD, restrict=False)
    service = FakeService(h)
    panel = ParentPanel(service)
    text, has_password = panel.status()
    assert has_password and text.startswith("Осталось 1 ч 0 мин")
    assert panel.run("bonus", "", 15) == (False, "Введите пароль родителя.")
    ok, text = panel.run("bonus", PASSWORD, 15)
    assert ok and "+15" in text
    ok, _ = panel.run("block", "")  # second action without typing the password again
    assert ok and service.sent[-1]["password"] == PASSWORD


def test_panel_without_password_set(handler):
    h, _ = handler
    text, has_password = ParentPanel(FakeService(h)).status()
    assert not has_password and "set-password" in text


def test_panel_service_down():
    panel = ParentPanel(lambda m: {"ok": False, "error": "unavailable",
                                   "message": "Служба родительского контроля не отвечает."})
    assert panel.status() == ("Служба родительского контроля не отвечает.", False)


def test_format_minutes():
    assert format_minutes(75.4) == "1 ч 15 мин" and format_minutes(-3) == "0 мин"
