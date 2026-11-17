"""The agent loop step: count time, enforce limits, watch processes, write the log.

All OS access goes through the `Platform` protocol, so this module is fully
testable with a fake platform.
"""

from __future__ import annotations

import fnmatch
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from pc_agent.activity import Activity, ActivityHub
from pc_agent.policy import Decision, evaluate
from pc_agent.rules_file import RulesFile
from pc_agent.storage import Store
from pc_shared.events import Event, EventKind
from pc_shared.rules import Rules

log = logging.getLogger(__name__)

NOTIFY_TITLE = "Родительский контроль"
GRACE_SECONDS = 60.0
# A larger gap between ticks means sleep/hibernation or a hang: do not count it as usage.
MAX_TICK_GAP_SECONDS = 30.0
# Do not restart a crashing helper more often than this.
HELPER_RESTART_SECONDS = 10.0
# Window focus shorter than this (alt-tab flicker) is not logged.
MIN_FOCUS_LOG_SECONDS = 2.0
# How long the local database keeps data (the server will keep its own copy).
EVENTS_KEEP_DAYS = 90
USAGE_KEEP_DAYS = 400

# System helpers running in every user session: not interesting in the activity log.
NOISE_PROCESSES = frozenset(
    {
        "applicationframehost.exe", "audiodg.exe", "backgroundtaskhost.exe", "conhost.exe",
        "csrss.exe", "ctfmon.exe", "dllhost.exe", "dwm.exe", "explorer.exe",
        "fontdrvhost.exe", "lockapp.exe", "logonui.exe", "runtimebroker.exe",
        "searchhost.exe", "searchprotocolhost.exe", "searchfilterhost.exe",
        "securityhealthsystray.exe", "shellexperiencehost.exe", "sihost.exe", "smartscreen.exe",
        "startmenuexperiencehost.exe", "svchost.exe", "taskhostw.exe", "textinputhost.exe",
        "useroobebroker.exe", "widgets.exe", "winlogon.exe", "wmiprvse.exe",
        "crossdeviceresume.exe", "phoneexperiencehost.exe",
    }
)


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    name: str
    exe: str | None
    create_time: float

    @property
    def key(self) -> tuple[int, float]:
        # PIDs are reused by Windows; together with the start time they are unique.
        return (self.pid, self.create_time)


class Platform(Protocol):
    def find_user_session(self, user: str) -> int | None: ...
    def is_session_active(self, session_id: int) -> bool: ...
    def list_session_processes(self, session_id: int) -> list[ProcInfo]: ...
    def kill(self, pid: int) -> bool: ...
    def notify(self, session_id: int, title: str, text: str) -> None: ...
    def logoff(self, session_id: int) -> bool: ...
    def lock(self, session_id: int) -> bool: ...
    def start_helper(self, session_id: int) -> int | None: ...
    def helper_alive(self, pid: int) -> bool: ...
    def timezone(self) -> str: ...
    def exe_names(self, exe: str) -> list[str]: ...


@dataclass(frozen=True)
class TickResult:
    session_id: int | None
    active: bool
    decision: Decision | None
    idle: bool = False
    app: str | None = None


def is_blocked(name: str, patterns: list[str]) -> bool:
    base = name.lower().removesuffix(".exe")
    return any(fnmatch.fnmatchcase(base, p) for p in patterns)


class Agent:
    def __init__(
        self,
        platform: Platform,
        store: Store,
        rules: RulesFile,
        hub: ActivityHub | None = None,
    ) -> None:
        self._platform = platform
        self._store = store
        self._rules = rules
        self._hub = hub
        self._helper_pid: int | None = None
        self._timezone: str | None = None
        self._purged_on: date | None = None
        self._helper_started_at: float | None = None
        self._focus: tuple[str, str] | None = None
        self._focus_since = 0.0
        self._prev_mono: float | None = None
        self._session_id: int | None = None
        self._known: dict[tuple[int, float], ProcInfo] = {}
        # How many processes of each program run: Chrome has dozens, but it is one program.
        self._running: dict[str, int] = {}
        self._scanned = False
        self._pending_since: float | None = None

    def _event(self, kind: EventKind, **data: object) -> None:
        self._store.add_event(Event(kind=kind, data=data))

    def _notify(self, session_id: int, text: str) -> None:
        try:
            self._platform.notify(session_id, NOTIFY_TITLE, text)
        except Exception:
            log.exception("Не удалось показать уведомление")

    def tick(self, now: datetime, mono: float) -> TickResult:
        """One step of the loop. `now` is wall-clock time, `mono` is time.monotonic()."""
        elapsed = 0.0
        if self._prev_mono is not None:
            elapsed = min(max(mono - self._prev_mono, 0.0), MAX_TICK_GAP_SECONDS)
        self._prev_mono = mono

        self._check_timezone()
        self._purge_old(now.date())
        rules = self._rules.get()
        if not rules.child_user:
            return TickResult(None, False, None)

        session_id = self._platform.find_user_session(rules.child_user)
        if session_id != self._session_id:
            self._session_changed()
            self._session_id = session_id
        if session_id is None:
            if self._hub is not None:
                self._hub.set_status({"child_logged_in": False})
            return TickResult(None, False, None)

        self._scan_processes(session_id, rules, mono)

        activity: Activity | None = None
        if self._hub is not None:
            self._ensure_helper(session_id, mono)
            activity = self._hub.latest(mono)

        active = self._platform.is_session_active(session_id)
        # Without fresh data from the helper the child is assumed to be present.
        idle = bool(
            activity
            and rules.idle_minutes
            and activity.idle_seconds >= rules.idle_minutes * 60
        )
        counting = active and not idle
        app = activity.app if activity else None
        # The interval since the previous tick belongs to the window focused during it.
        previous_app = self._focus[0] if self._focus else None
        today = now.date()
        if counting and elapsed > 0:
            self._store.add_usage(today, elapsed)
            if previous_app:
                self._store.add_app_usage(today, previous_app, elapsed)
        self._track_focus(activity if counting else None, mono)

        state = self._store.load_day(today)
        decision = evaluate(rules, state, now)
        if decision.allowed:
            self._pending_since = None
            if decision.warn_now:
                minutes = max(1, math.ceil(decision.remaining_minutes))
                self._notify(
                    session_id,
                    f"Осталось примерно {minutes} мин. работы за компьютером. "
                    "Сохрани свою работу заранее.",
                )
                self._event(EventKind.WARNING, remaining_minutes=minutes)
            if decision.warned_after != state.warned:
                self._store.set_warned(today, decision.warned_after)
        else:
            self._enforce(session_id, rules, decision, active, mono)

        if self._hub is not None:
            self._hub.set_status(
                {
                    "allowed": decision.allowed,
                    "reason": decision.reason,
                    "remaining_minutes": round(decision.remaining_minutes, 1),
                    "used_minutes": round(decision.used_minutes, 1),
                    "limit_minutes": decision.limit_minutes,
                    "window": decision.window,
                    "idle": idle,
                    "child_logged_in": True,
                }
            )
        return TickResult(session_id, active, decision, idle=idle, app=app)

    def _purge_old(self, today: date) -> None:
        if self._purged_on == today:
            return
        self._purged_on = today
        deleted = self._store.purge(today, EVENTS_KEEP_DAYS, USAGE_KEEP_DAYS)
        if deleted:
            log.info("Удалено старых записей журнала: %s", deleted)

    def _check_timezone(self) -> None:
        """Log a time zone change: it shifts the allowed hours and the start of the day.

        Compared with the value saved in the database, so a change made before a
        reboot is noticed too.
        """
        current = self._platform.timezone()
        if not current or current == self._timezone:
            return
        saved = self._store.get_setting("timezone")
        if saved and saved != current:
            log.warning("Часовой пояс изменён: %s -> %s", saved, current)
            self._event(EventKind.TIMEZONE_CHANGED, old=saved, new=current)
        if saved != current:
            self._store.set_setting("timezone", current)
        self._timezone = current

    def _ensure_helper(self, session_id: int, mono: float) -> None:
        """Keep the helper running in the child's session; restart it if it was closed."""
        assert self._hub is not None
        if self._helper_pid is not None:
            if self._platform.helper_alive(self._helper_pid):
                return
            log.warning("Helper (процесс %s) завершился", self._helper_pid)
            self._event(EventKind.HELPER_STOPPED, pid=self._helper_pid)
            self._helper_pid = None
            self._hub.set_expected_pid(None)
        if (
            self._helper_started_at is not None
            and mono - self._helper_started_at < HELPER_RESTART_SECONDS
        ):
            return
        self._helper_started_at = mono
        try:
            pid = self._platform.start_helper(session_id)
        except Exception as exc:
            log.exception("Не удалось запустить helper")
            self._event(EventKind.ERROR, message=f"helper: {exc!r}")
            return
        if pid is None:
            return
        self._helper_pid = pid
        self._hub.set_expected_pid(pid)
        log.info("Helper запущен в сеансе %s (процесс %s)", session_id, pid)
        self._event(EventKind.HELPER_STARTED, pid=pid, session_id=session_id)

    def _track_focus(self, activity: Activity | None, mono: float) -> None:
        """Log each window the child looked at, with how long it was in focus."""
        current = (activity.app or "?", activity.title) if activity else None
        if current == self._focus:
            return
        if self._focus is not None:
            seconds = mono - self._focus_since
            if seconds >= MIN_FOCUS_LOG_SECONDS:
                app, title = self._focus
                self._event(EventKind.WINDOW_FOCUS, app=app, title=title, seconds=round(seconds))
        self._focus = current
        self._focus_since = mono

    def _enforce(
        self, session_id: int, rules: Rules, decision: Decision, active: bool, mono: float
    ) -> None:
        # In "lock" mode a session that is already on the lock screen needs nothing more.
        if rules.enforcement == "lock" and not active:
            self._pending_since = None
            return
        if self._pending_since is None:
            self._pending_since = mono
            action = "завершён" if rules.enforcement == "logoff" else "заблокирован"
            self._notify(
                session_id,
                f"{decision.reason}\n\nЧерез 1 минуту сеанс будет {action}. "
                "Сохрани свою работу!",
            )
            self._event(EventKind.WARNING, reason=decision.reason, final=True)
            return
        if mono - self._pending_since < GRACE_SECONDS:
            return
        self._pending_since = None
        if rules.enforcement == "logoff":
            ok = self._platform.logoff(session_id)
        else:
            ok = self._platform.lock(session_id)
        log.info("Сеанс %s: %s (%s)", session_id, rules.enforcement, decision.reason)
        self._event(
            EventKind.SESSION_ENDED, action=rules.enforcement, reason=decision.reason, ok=ok
        )

    def _session_changed(self) -> None:
        self._known.clear()
        self._running.clear()
        self._scanned = False
        self._pending_since = None
        self._track_focus(None, self._prev_mono or 0.0)
        # The old helper ends with the old session; start a new one right away.
        self._helper_pid = None
        self._helper_started_at = None
        if self._hub is not None:
            self._hub.set_expected_pid(None)

    def _is_blocked_process(self, proc: ProcInfo, patterns: list[str]) -> bool:
        """By file name, and by the names inside the exe (original file name, product,
        description) which stay the same when the file is renamed."""
        if is_blocked(proc.name, patterns):
            return True
        if not proc.exe:
            return False
        try:
            names = self._platform.exe_names(proc.exe)
        except Exception:
            log.debug("Не удалось прочитать сведения о файле %s", proc.exe, exc_info=True)
            return False
        return any(is_blocked(name, patterns) for name in names)

    def _scan_processes(self, session_id: int, rules: Rules, mono: float) -> None:
        first_scan = not self._scanned
        self._scanned = True
        current = {p.key: p for p in self._platform.list_session_processes(session_id)}

        killed: list[str] = []
        for key, proc in current.items():
            if key in self._known:
                continue
            if rules.blocked_apps and self._is_blocked_process(proc, rules.blocked_apps):
                ok = self._platform.kill(proc.pid)
                killed.append(proc.name)
                self._event(EventKind.APP_BLOCKED, name=proc.name, exe=proc.exe, killed=ok)
                if not ok:
                    # Remember it so a process we cannot kill is not retried every tick.
                    self._known[key] = proc
                continue
            self._known[key] = proc
            name = proc.name.lower()
            self._running[name] = self._running.get(name, 0) + 1
            # Programs already open when the agent starts are not "started" now.
            if self._running[name] == 1 and not first_scan and name not in NOISE_PROCESSES:
                self._event(EventKind.PROCESS_STARTED, name=proc.name, exe=proc.exe)

        for key in list(self._known):
            if key not in current:
                proc = self._known.pop(key)
                name = proc.name.lower()
                if name not in self._running:
                    continue  # a blocked process we could not kill
                self._running[name] -= 1
                if self._running[name] == 0:
                    del self._running[name]
                    if name not in NOISE_PROCESSES:
                        self._event(EventKind.PROCESS_STOPPED, name=proc.name)

        if killed:
            names = ", ".join(sorted(set(killed)))
            log.info("Закрыты запрещённые программы: %s", names)
            self._notify(session_id, f"Эта программа запрещена родителями: {names}")
