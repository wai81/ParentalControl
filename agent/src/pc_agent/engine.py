"""The agent loop step: count time, enforce limits, watch processes, write the log.

All OS access goes through the `Platform` protocol, so this module is fully
testable with a fake platform.
"""

from __future__ import annotations

import fnmatch
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

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


@dataclass(frozen=True)
class TickResult:
    session_id: int | None
    active: bool
    decision: Decision | None


def is_blocked(name: str, patterns: list[str]) -> bool:
    base = name.lower().removesuffix(".exe")
    return any(fnmatch.fnmatchcase(base, p) for p in patterns)


class Agent:
    def __init__(self, platform: Platform, store: Store, rules: RulesFile) -> None:
        self._platform = platform
        self._store = store
        self._rules = rules
        self._prev_mono: float | None = None
        self._session_id: int | None = None
        self._known: dict[tuple[int, float], ProcInfo] = {}
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

        rules = self._rules.get()
        if not rules.child_user:
            return TickResult(None, False, None)

        session_id = self._platform.find_user_session(rules.child_user)
        if session_id != self._session_id:
            self._session_changed()
            self._session_id = session_id
        if session_id is None:
            return TickResult(None, False, None)

        self._scan_processes(session_id, rules, mono)

        active = self._platform.is_session_active(session_id)
        today = now.date()
        if active and elapsed > 0:
            self._store.add_usage(today, elapsed)

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

        return TickResult(session_id, active, decision)

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
        for proc in self._known.values():
            if proc.name.lower() not in NOISE_PROCESSES:
                self._event(
                    EventKind.PROCESS_STOPPED, name=proc.name, pid=proc.pid, session_end=True
                )
        self._known.clear()
        self._scanned = False
        self._pending_since = None

    def _scan_processes(self, session_id: int, rules: Rules, mono: float) -> None:
        first_scan = not self._scanned
        self._scanned = True
        current = {p.key: p for p in self._platform.list_session_processes(session_id)}

        killed: list[str] = []
        for key, proc in current.items():
            if key in self._known:
                continue
            if rules.blocked_apps and is_blocked(proc.name, rules.blocked_apps):
                ok = self._platform.kill(proc.pid)
                killed.append(proc.name)
                self._event(EventKind.APP_BLOCKED, name=proc.name, exe=proc.exe, killed=ok)
                if not ok:
                    # Remember it so a process we cannot kill is not retried every tick.
                    self._known[key] = proc
                continue
            self._known[key] = proc
            if proc.name.lower() not in NOISE_PROCESSES:
                self._event(
                    EventKind.PROCESS_STARTED,
                    name=proc.name,
                    exe=proc.exe,
                    pid=proc.pid,
                    already_running=first_scan,
                )

        for key in list(self._known):
            if key not in current:
                proc = self._known.pop(key)
                if proc.name.lower() not in NOISE_PROCESSES:
                    self._event(EventKind.PROCESS_STOPPED, name=proc.name, pid=proc.pid)

        if killed:
            names = ", ".join(sorted(set(killed)))
            log.info("Закрыты запрещённые программы: %s", names)
            self._notify(session_id, f"Эта программа запрещена родителями: {names}")
