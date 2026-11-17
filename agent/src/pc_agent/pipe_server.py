"""Named pipe server in the service: receives reports from the helper."""

from __future__ import annotations

import contextlib
import logging
import threading
import time

import pywintypes
import win32file
import win32pipe
import win32security

from pc_agent.activity import PIPE_NAME, ActivityHub, decode, encode, parse_report

log = logging.getLogger(__name__)

# SYSTEM and Administrators: full access; interactive users: read/write (connect only).
_PIPE_SDDL = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGW;;;IU)"
_BUFFER = 64 * 1024
_ERROR_BROKEN_PIPE = 109
_ERROR_MORE_DATA = 234


def _security_attributes() -> pywintypes.SECURITY_ATTRIBUTES:
    sa = pywintypes.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        _PIPE_SDDL, win32security.SDDL_REVISION_1
    )
    return sa


class PipeServer(threading.Thread):
    def __init__(self, hub: ActivityHub, name: str = PIPE_NAME) -> None:
        super().__init__(name="pipe-server", daemon=True)
        self._hub = hub
        self._name = name
        self._stop_event = threading.Event()
        self._rejected_log_at = 0.0

    def stop(self) -> None:
        self._stop_event.set()
        # Unblock ConnectNamedPipe by connecting to ourselves; we get rejected and exit.
        try:
            handle = win32file.CreateFile(
                self._name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None,
            )
            handle.Close()
        except pywintypes.error:
            pass
        self.join(timeout=5)

    def run(self) -> None:
        sa = _security_attributes()
        while not self._stop_event.is_set():
            try:
                pipe = win32pipe.CreateNamedPipe(
                    self._name,
                    win32pipe.PIPE_ACCESS_DUPLEX,
                    win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE
                    | win32pipe.PIPE_WAIT | win32pipe.PIPE_REJECT_REMOTE_CLIENTS,
                    1, _BUFFER, _BUFFER, 0, sa,
                )
            except pywintypes.error:
                log.exception("Не удалось создать канал %s", self._name)
                self._stop_event.wait(5)
                continue
            try:
                self._serve(pipe)
            except Exception:
                log.exception("Ошибка канала связи с helper")
            finally:
                with contextlib.suppress(pywintypes.error):
                    win32pipe.DisconnectNamedPipe(pipe)
                pipe.Close()

    def _serve(self, pipe) -> None:  # noqa: ANN001 (PyHANDLE)
        try:
            win32pipe.ConnectNamedPipe(pipe, None)
        except pywintypes.error as exc:
            if exc.winerror != 535:  # ERROR_PIPE_CONNECTED: client was faster, that's fine
                raise
        if self._stop_event.is_set():
            return
        client_pid = win32pipe.GetNamedPipeClientProcessId(pipe)
        if not self._hub.accepts(client_pid):
            now = time.monotonic()
            if now - self._rejected_log_at > 60:
                log.warning("Отклонено подключение к каналу от процесса %s", client_pid)
                self._rejected_log_at = now
            return
        log.info("Helper подключился (процесс %s)", client_pid)
        while not self._stop_event.is_set():
            try:
                _, data = win32file.ReadFile(pipe, _BUFFER)
            except pywintypes.error as exc:
                if exc.winerror == _ERROR_MORE_DATA:
                    continue  # oversized message: ignore it
                if exc.winerror == _ERROR_BROKEN_PIPE:
                    log.info("Helper отключился")
                    return
                raise
            try:
                self._hub.push(parse_report(decode(data), time.monotonic()))
            except (ValueError, TypeError) as exc:
                log.warning("Неверное сообщение от helper: %s", exc)
            win32file.WriteFile(pipe, encode(self._hub.status()))
