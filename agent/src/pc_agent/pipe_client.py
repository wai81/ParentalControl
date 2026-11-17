"""Client side of the service pipes (used by the helper, the CLI and the parent dialog)."""

from __future__ import annotations

import os
from typing import Any

import pywintypes
import win32file
import win32pipe

from pc_agent import winapi
from pc_agent.activity import decode, encode
from pc_agent.control import CONTROL_PIPE_NAME

# Development only: accept a pipe served by `pcagent run` in a user session.
DEV_ENV = "PC_AGENT_DEV"
_BUFFER = 64 * 1024


class NotTheService(ConnectionError):
    pass


class PipeClient:
    def __init__(self, name: str, *, require_service: bool = False) -> None:
        self._name = name
        self._require_service = require_service
        self._handle = None

    def _connect(self) -> None:
        handle = win32file.CreateFile(
            self._name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0, None, win32file.OPEN_EXISTING, 0, None,
        )
        win32pipe.SetNamedPipeHandleState(handle, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        if self._require_service and not _served_by_service(handle):
            handle.Close()
            raise NotTheService(
                "Канал создан не службой родительского контроля. "
                "Пароль не отправлен. Проверьте, что служба запущена."
            )
        self._handle = handle

    def close(self) -> None:
        if self._handle is not None:
            self._handle.Close()
            self._handle = None

    def request(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Send a message and return the reply, or None if the service is not reachable."""
        try:
            if self._handle is None:
                self._connect()
            win32file.WriteFile(self._handle, encode(message))
            _, data = win32file.ReadFile(self._handle, _BUFFER)
            return decode(data)
        except (pywintypes.error, ValueError):
            self.close()
            return None


def _served_by_service(handle) -> bool:  # noqa: ANN001 (PyHANDLE)
    """Services run in session 0, where a child's program cannot start."""
    if os.environ.get(DEV_ENV) == "1":
        return True
    try:
        pid = win32pipe.GetNamedPipeServerProcessId(handle)
    except pywintypes.error:
        return False
    return winapi.process_session_id(pid) == 0


def send_command(message: dict[str, Any]) -> dict[str, Any]:
    """Send a parent command to the service. Never sends the password to an impostor."""
    client = PipeClient(CONTROL_PIPE_NAME, require_service=True)
    try:
        reply = client.request(message)
    except NotTheService as exc:
        return {"ok": False, "error": "impostor", "message": str(exc)}
    finally:
        client.close()
    if reply is None:
        return {
            "ok": False, "error": "unavailable",
            "message": "Служба родительского контроля не отвечает.",
        }
    return reply
