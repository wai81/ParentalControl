"""Named pipe servers in the service.

Two pipes use this class:
- the report pipe: activity from the helper (only the helper the service started);
- the control pipe: parent commands, password-protected (anyone may connect).

Each connection is served in its own thread, so a client that connects and
sends nothing cannot block the pipe for others.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import pywintypes
import win32file
import win32pipe
import win32security

from pc_agent.activity import ActivityHub, decode, encode, parse_report

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], dict[str, Any]]

# SYSTEM and Administrators: full access; interactive users: read/write (connect only).
_PIPE_SDDL = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;IU)"
_BUFFER = 64 * 1024
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_ERROR_ACCESS_DENIED = 5
_ERROR_BROKEN_PIPE = 109
_ERROR_MORE_DATA = 234
_ERROR_PIPE_CONNECTED = 535


def _security_attributes() -> pywintypes.SECURITY_ATTRIBUTES:
    sa = pywintypes.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        _PIPE_SDDL, win32security.SDDL_REVISION_1
    )
    return sa


def report_handler(hub: ActivityHub) -> Handler:
    def handle(message: dict[str, Any]) -> dict[str, Any]:
        hub.push(parse_report(message, time.monotonic()))
        return hub.status()

    return handle


class PipeServer(threading.Thread):
    def __init__(
        self,
        name: str,
        handler: Handler,
        *,
        accept: Callable[[int], bool] | None = None,
        label: str = "Клиент",
        max_clients: int = 8,
    ) -> None:
        super().__init__(name=f"pipe {name}", daemon=True)
        self._name = name
        self._handler = handler
        self._accept = accept
        self._label = label
        self._slots = threading.Semaphore(max_clients)
        self._stop_event = threading.Event()
        self._rejected_log_at = 0.0

    def stop(self) -> None:
        self._stop_event.set()
        # Unblock ConnectNamedPipe by connecting to ourselves.
        with contextlib.suppress(pywintypes.error):
            win32file.CreateFile(
                self._name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None,
            ).Close()
        self.join(timeout=5)

    def run(self) -> None:
        sa = _security_attributes()
        first = True
        while not self._stop_event.is_set():
            try:
                # The first instance must be ours: if another process already owns the
                # name (pipe squatting, e.g. to catch the parent's password), fail loudly.
                mode = win32pipe.PIPE_ACCESS_DUPLEX
                if first:
                    mode |= _FILE_FLAG_FIRST_PIPE_INSTANCE
                pipe = win32pipe.CreateNamedPipe(
                    self._name, mode,
                    win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE
                    | win32pipe.PIPE_WAIT | win32pipe.PIPE_REJECT_REMOTE_CLIENTS,
                    win32pipe.PIPE_UNLIMITED_INSTANCES, _BUFFER, _BUFFER, 0, sa,
                )
                first = False
            except pywintypes.error as exc:
                if first and exc.winerror == _ERROR_ACCESS_DENIED:
                    log.error("Канал %s уже создан другим процессом", self._name)
                else:
                    log.exception("Не удалось создать канал %s", self._name)
                self._stop_event.wait(5)
                continue
            try:
                win32pipe.ConnectNamedPipe(pipe, None)
            except pywintypes.error as exc:
                if exc.winerror != _ERROR_PIPE_CONNECTED:  # client was faster: that's fine
                    pipe.Close()
                    continue
            if self._stop_event.is_set():
                pipe.Close()
                break
            client_pid = win32pipe.GetNamedPipeClientProcessId(pipe)
            if (self._accept and not self._accept(client_pid)) or not self._slots.acquire(
                blocking=False
            ):
                self._reject(pipe, client_pid)
                continue
            threading.Thread(
                target=self._serve, args=(pipe, client_pid), daemon=True
            ).start()

    def _reject(self, pipe, client_pid: int) -> None:  # noqa: ANN001 (PyHANDLE)
        now = time.monotonic()
        if now - self._rejected_log_at > 60:
            log.warning("Отклонено подключение к %s от процесса %s", self._name, client_pid)
            self._rejected_log_at = now
        with contextlib.suppress(pywintypes.error):
            win32pipe.DisconnectNamedPipe(pipe)
        pipe.Close()

    def _serve(self, pipe, client_pid: int) -> None:  # noqa: ANN001 (PyHANDLE)
        log.info("%s подключился (процесс %s)", self._label, client_pid)
        try:
            while not self._stop_event.is_set():
                try:
                    _, data = win32file.ReadFile(pipe, _BUFFER)
                except pywintypes.error as exc:
                    if exc.winerror == _ERROR_MORE_DATA:
                        continue  # oversized message: ignore it
                    if exc.winerror == _ERROR_BROKEN_PIPE:
                        return
                    raise
                try:
                    reply = self._handler(decode(data))
                except (ValueError, TypeError, KeyError) as exc:
                    log.warning("Неверное сообщение в %s: %s", self._name, exc)
                    reply = {"ok": False, "error": "bad_request", "message": "Неверный запрос."}
                except Exception:
                    log.exception("Ошибка обработки запроса в %s", self._name)
                    reply = {"ok": False, "error": "internal", "message": "Ошибка службы."}
                win32file.WriteFile(pipe, encode(reply))
        except pywintypes.error as exc:
            log.info("%s отключился: %s", self._label, exc)
        finally:
            with contextlib.suppress(pywintypes.error):
                win32pipe.DisconnectNamedPipe(pipe)
            pipe.Close()
            self._slots.release()
