"""Windows implementation of the engine's Platform protocol."""

from __future__ import annotations

import logging
import os
import subprocess
import sys

import psutil
import win32con
import win32event
import win32process
import win32profile
import win32ts

from pc_agent import winapi
from pc_agent.engine import ProcInfo

log = logging.getLogger(__name__)


def helper_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "helper"]
    return [sys.executable, "-m", "pc_agent", "helper"]


def _user_matches(rule_user: str, session: winapi.Session) -> bool:
    """`rule_user` is "name" or "DOMAIN\\name"; comparison is case-insensitive."""
    if "\\" in rule_user:
        domain, name = rule_user.split("\\", 1)
        return (
            name.casefold() == session.user.casefold()
            and domain.casefold() == session.domain.casefold()
        )
    return rule_user.casefold() == session.user.casefold()


class WindowsPlatform:
    def __init__(self) -> None:
        self._helpers: dict[int, object] = {}  # pid -> process handle or Popen

    def find_user_session(self, user: str) -> int | None:
        matches = [s for s in winapi.list_sessions() if _user_matches(user, s)]
        if not matches:
            return None
        console = winapi.active_console_session()
        for s in matches:
            if s.session_id == console:
                return s.session_id
        return matches[0].session_id

    def is_session_active(self, session_id: int) -> bool:
        """The child's session is on the screen and not locked."""
        if winapi.active_console_session() != session_id:
            return False
        try:
            locked = winapi.is_session_locked(session_id)
        except OSError:
            locked = None
        if locked is None:
            locked = self._logonui_running(session_id)
        return not locked

    def _logonui_running(self, session_id: int) -> bool:
        for proc in psutil.process_iter(["name"]):
            if (proc.info["name"] or "").lower() == "logonui.exe" and (
                winapi.process_session_id(proc.pid) == session_id
            ):
                return True
        return False

    def list_session_processes(self, session_id: int) -> list[ProcInfo]:
        result = []
        for proc in psutil.process_iter(["name", "exe", "create_time"]):
            if winapi.process_session_id(proc.pid) != session_id:
                continue
            info = proc.info
            result.append(
                ProcInfo(
                    pid=proc.pid,
                    name=info["name"] or "",
                    exe=info["exe"],
                    create_time=info["create_time"] or 0.0,
                )
            )
        return result

    def kill(self, pid: int) -> bool:
        try:
            psutil.Process(pid).kill()
            return True
        except psutil.NoSuchProcess:
            return True
        except psutil.Error:
            log.warning("Не удалось закрыть процесс %s", pid, exc_info=True)
            return False

    def notify(self, session_id: int, title: str, text: str) -> None:
        if not winapi.send_message(session_id, title, text):
            log.warning("WTSSendMessage не сработал для сеанса %s", session_id)

    def logoff(self, session_id: int) -> bool:
        return winapi.logoff_session(session_id)

    def lock(self, session_id: int) -> bool:
        return winapi.disconnect_session(session_id)

    def start_helper(self, session_id: int) -> int | None:
        """Start the helper in `session_id` as the user of that session.

        As a SYSTEM service this uses the session's user token. When the agent runs
        in a console inside that same session (development), a plain child process
        is enough.
        """
        command = helper_command()
        if winapi.process_session_id(os.getpid()) == session_id:
            proc = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
            self._helpers[proc.pid] = proc
            return proc.pid
        token = win32ts.WTSQueryUserToken(session_id)
        try:
            env = win32profile.CreateEnvironmentBlock(token, False)
            startup = win32process.STARTUPINFO()
            startup.lpDesktop = r"winsta0\default"
            process, thread, pid, _ = win32process.CreateProcessAsUser(
                token, None, subprocess.list2cmdline(command), None, None, False,
                win32con.CREATE_NO_WINDOW | win32con.CREATE_UNICODE_ENVIRONMENT,
                env, os.path.dirname(command[0]), startup,
            )
            thread.Close()
        finally:
            token.Close()
        self._helpers[pid] = process
        return pid

    def helper_alive(self, pid: int) -> bool:
        handle = self._helpers.get(pid)
        if handle is None:
            return False
        if isinstance(handle, subprocess.Popen):
            alive = handle.poll() is None
        else:
            alive = win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_TIMEOUT
        if not alive:
            del self._helpers[pid]
        return alive


class DryRunPlatform(WindowsPlatform):
    """Observes and notifies, but never ends sessions or kills processes."""

    def kill(self, pid: int) -> bool:
        log.info("[dry-run] процесс %s был бы закрыт", pid)
        return False  # not killed: the engine remembers it and does not retry every tick

    def logoff(self, session_id: int) -> bool:
        log.info("[dry-run] сеанс %s был бы завершён", session_id)
        return True

    def lock(self, session_id: int) -> bool:
        log.info("[dry-run] сеанс %s был бы заблокирован", session_id)
        return True
