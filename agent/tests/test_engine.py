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
        self.helper_starts: list[int] = []
        self.helper_running = True
        self.next_helper_pid = 500

    def start_helper(self, session_id):
        self.next_helper_pid += 1
        self.helper_starts.append(self.next_helper_pid)
        self.helper_running = True
        return self.next_helper_pid

    def helper_alive(self, pid):
        return self.helper_running

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


# ---------- helper: idle, app time, focus log, supervision ----------

from pc_agent.activity import Activity, ActivityHub  # noqa: E402


@pytest.fixture
def hub_env(tmp_path):
    rules_path = tmp_path / "rules.json"
    save_rules(rules_path, Rules(
        child_user="kid",
        weekdays=DayRules(limit_minutes=60, window=Window(start="08:00", end="21:00")),
        idle_minutes=3,
    ))
    store = Store(tmp_path / "agent.db")
    platform = FakePlatform()
    hub = ActivityHub()
    agent = Agent(platform, store, RulesFile(rules_path), hub)
    yield agent, platform, store, hub
    store.close()


def report(hub, mono, app="game.exe", title="Game", idle=0.0):
    hub.push(Activity(idle_seconds=idle, app=app, title=title, received=mono))


def test_idle_time_is_not_counted(hub_env):
    agent, platform, store, hub = hub_env
    report(hub, 0)
    agent.tick(NOON, 0)
    report(hub, 5, idle=10)
    agent.tick(NOON, 5)
    report(hub, 10, idle=200)  # over 3 minutes without input
    result = agent.tick(NOON, 10)
    assert result.idle
    assert store.load_day(MONDAY).used_seconds == pytest.approx(5)
    assert hub.status()["idle"] is True


def test_stale_report_means_present(hub_env):
    agent, platform, store, hub = hub_env
    report(hub, 0, idle=999)
    agent.tick(NOON, 0)
    agent.tick(NOON, 20)  # report is 20 s old: ignored, time is counted
    assert store.load_day(MONDAY).used_seconds == pytest.approx(20)


def test_app_usage_and_focus_log(hub_env):
    agent, platform, store, hub = hub_env
    agent.tick(NOON, -1)  # starts the helper; reports before that are discarded
    report(hub, 0, app="chrome.exe", title="YouTube")
    agent.tick(NOON, 0)
    report(hub, 5, app="chrome.exe", title="YouTube")
    agent.tick(NOON, 5)
    report(hub, 10, app="Minecraft.exe", title="Minecraft")
    agent.tick(NOON, 10)
    report(hub, 11, app="explorer.exe", title="flicker")
    agent.tick(NOON, 11)
    report(hub, 12, app="Minecraft.exe", title="Minecraft")
    agent.tick(NOON, 12)
    report(hub, 15, app="Minecraft.exe", title="Minecraft")
    agent.tick(NOON, 15)

    usage = dict(store.app_usage(MONDAY))
    assert usage["chrome.exe"] == pytest.approx(10 / 60)
    assert usage["minecraft.exe"] == pytest.approx(4 / 60)
    assert usage["explorer.exe"] == pytest.approx(1 / 60)
    focus = [e.data for e in store.recent_events(100) if e.kind == EventKind.WINDOW_FOCUS]
    assert focus == [{"app": "chrome.exe", "title": "YouTube", "seconds": 10}]  # flicker skipped


def test_status_published_for_tray(hub_env):
    agent, platform, store, hub = hub_env
    store.add_usage(MONDAY, 50 * 60)
    agent.tick(NOON, 0)
    status = hub.status()
    assert status["known"] and status["allowed"]
    assert status["remaining_minutes"] == pytest.approx(10)


def test_helper_restarted_after_kill(hub_env):
    agent, platform, store, hub = hub_env
    agent.tick(NOON, 0)
    assert platform.helper_starts == [501]
    platform.helper_running = False
    agent.tick(NOON, 5)  # noticed, but restart is rate-limited
    agent.tick(NOON, 11)
    assert platform.helper_starts == [501, 502]
    events = kinds(store)
    assert events.count(EventKind.HELPER_STARTED) == 2
    assert EventKind.HELPER_STOPPED in events


def test_new_session_starts_new_helper(hub_env):
    agent, platform, store, hub = hub_env
    agent.tick(NOON, 0)
    platform.session = 3
    agent.tick(NOON, 1)
    assert platform.helper_starts == [501, 502]


def test_hub_accepts_only_expected_process():
    import os

    hub = ActivityHub()
    assert not hub.accepts(os.getpid())
    hub.set_expected_pid(os.getpid())
    assert hub.accepts(os.getpid())
    hub.set_expected_pid(os.getppid())  # we are a child of the expected process
    assert hub.accepts(os.getpid())
    hub.set_expected_pid(999_999)
    assert not hub.accepts(os.getpid())
