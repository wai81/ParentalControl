from datetime import date, datetime

import pytest

from pc_agent.engine import GRACE_SECONDS, Agent, ProcInfo, is_blocked
from pc_agent.rules_file import RulesFile, save_rules
from pc_agent.storage import Store
from pc_shared.events import EventKind
from pc_shared.rules import DayRules, Rules, Window

MONDAY = date(2026, 11, 16)
NOON = datetime(2026, 11, 16, 12, 0)


class FakePlatform:
    def __init__(self) -> None:
        self.session: int | None = 2
        self.active = True
        self.procs: list[ProcInfo] = []
        self.killed: list[int] = []
        self.kill_ok = True
        self.notes: list[str] = []
        self.logoffs: list[int] = []
        self.locks: list[int] = []

    def find_user_session(self, user):
        return self.session

    def is_session_active(self, session_id):
        return self.active

    def list_session_processes(self, session_id):
        return [p for p in self.procs if p.pid not in self.killed]

    def kill(self, pid):
        if self.kill_ok:
            self.killed.append(pid)
        return self.kill_ok

    def notify(self, session_id, title, text):
        self.notes.append(text)

    def logoff(self, session_id):
        self.logoffs.append(session_id)
        return True

    def lock(self, session_id):
        self.locks.append(session_id)
        return True


@pytest.fixture
def env(tmp_path):
    rules_path = tmp_path / "rules.json"
    save_rules(rules_path, Rules(
        child_user="kid",
        weekdays=DayRules(limit_minutes=60, window=Window(start="08:00", end="21:00")),
        blocked_apps=["roblox*"],
    ))
    store = Store(tmp_path / "agent.db")
    platform = FakePlatform()
    agent = Agent(platform, store, RulesFile(rules_path))
    yield agent, platform, store, rules_path
    store.close()


def kinds(store):
    return [e.kind for e in store.recent_events(500)]


def test_counts_only_active_time_and_caps_gaps(env):
    agent, platform, store, _ = env
    agent.tick(NOON, 0)
    agent.tick(NOON, 5)
    platform.active = False
    agent.tick(NOON, 10)
    platform.active = True
    agent.tick(NOON, 3600)  # sleep for an hour: counted as at most 30 s
    assert store.load_day(MONDAY).used_seconds == pytest.approx(5 + 30)


def test_logoff_after_grace_period(env):
    agent, platform, store, _ = env
    store.add_usage(MONDAY, 60 * 60)
    agent.tick(NOON, 0)
    assert len(platform.notes) == 1 and "1 минуту" in platform.notes[0]
    agent.tick(NOON, GRACE_SECONDS - 1)
    assert platform.logoffs == []
    agent.tick(NOON, GRACE_SECONDS + 1)
    assert platform.logoffs == [2]
    assert EventKind.SESSION_ENDED in kinds(store)


def test_bonus_cancels_pending_logoff(env):
    agent, platform, store, _ = env
    store.add_usage(MONDAY, 60 * 60)
    agent.tick(NOON, 0)
    store.add_bonus(MONDAY, 15)
    agent.tick(NOON, GRACE_SECONDS + 1)
    assert platform.logoffs == []


def test_lock_mode_skips_already_locked_session(env, tmp_path):
    agent, platform, store, rules_path = env
    save_rules(rules_path, Rules(child_user="kid", enforcement="lock",
                                 weekdays=DayRules(limit_minutes=0)))
    platform.active = False
    agent.tick(NOON, 0)
    agent.tick(NOON, 100)
    assert platform.notes == [] and platform.locks == []


def test_warning_shown_once(env):
    agent, platform, store, _ = env
    store.add_usage(MONDAY, 52 * 60)
    agent.tick(NOON, 0)
    agent.tick(NOON, 5)
    assert len(platform.notes) == 1 and "Осталось примерно 8 мин" in platform.notes[0]
    assert store.load_day(MONDAY).warned == {10}


def test_process_log_and_blocking(env):
    agent, platform, store, _ = env
    platform.procs = [ProcInfo(10, "notepad.exe", r"C:\Windows\notepad.exe", 1.0),
                      ProcInfo(11, "svchost.exe", None, 1.0)]
    agent.tick(NOON, 0)
    platform.procs.append(ProcInfo(12, "RobloxPlayerBeta.exe", None, 2.0))
    agent.tick(NOON, 5)
    platform.procs = [p for p in platform.procs if p.pid != 10]
    agent.tick(NOON, 10)

    events = store.recent_events(500)
    started = [e for e in events if e.kind == EventKind.PROCESS_STARTED]
    assert [e.data["name"] for e in started] == ["notepad.exe"]  # svchost is noise
    assert started[0].data["already_running"] is True
    assert platform.killed == [12]
    assert any(e.kind == EventKind.APP_BLOCKED for e in events)
    assert any(e.kind == EventKind.PROCESS_STOPPED and e.data["name"] == "notepad.exe"
               for e in events)
    assert any("запрещена" in n for n in platform.notes)


def test_unkillable_process_is_not_retried(env):
    agent, platform, store, _ = env
    platform.kill_ok = False
    platform.procs = [ProcInfo(12, "roblox.exe", None, 2.0)]
    for t in range(0, 30, 5):
        agent.tick(NOON, t)
    assert kinds(store).count(EventKind.APP_BLOCKED) == 1


def test_no_session_does_nothing(env):
    agent, platform, store, _ = env
    platform.session = None
    agent.tick(NOON, 0)
    agent.tick(NOON, 5)
    assert store.load_day(MONDAY).used_seconds == 0


def test_broken_rules_keep_previous(env):
    agent, platform, store, rules_path = env
    errors = []
    rf = RulesFile(rules_path, on_error=errors.append)
    assert rf.get().child_user == "kid"
    rules_path.write_text("{ broken", encoding="utf-8")
    import os
    os.utime(rules_path, ns=(1, 1))
    assert rf.get().child_user == "kid"
    assert errors


@pytest.mark.parametrize(("name", "expected"), [
    ("RobloxPlayerBeta.exe", True), ("roblox", True), ("notepad.exe", False),
])
def test_is_blocked(name, expected):
    assert is_blocked(name, ["roblox*"]) is expected
