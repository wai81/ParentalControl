"""Helper running in the child's session as the child (started by the service).

Every few seconds it tells the service which window is in focus and how long
there has been no keyboard/mouse input, and shows the remaining time in the tray.
It has no "Exit" menu item; if it is killed, the service starts it again.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from typing import Any

import psutil
import pywintypes
import win32api
import win32event
import win32gui
import win32process
import winerror

from pc_agent.activity import PIPE_NAME, REPORT_INTERVAL
from pc_agent.pipe_client import PipeClient

log = logging.getLogger(__name__)

_MUTEX_NAME = "Local\\ParentalControlHelper"
NO_CONTACT_EXIT_SECONDS = 15.0
_TITLE = "Родительский контроль"


# ---------- what the child is doing ----------


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds() -> float:
    info = _LASTINPUTINFO(cbSize=ctypes.sizeof(_LASTINPUTINFO))
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0


def _process_name(pid: int) -> str | None:
    try:
        return psutil.Process(pid).name()
    except psutil.Error:
        return None


def foreground_window() -> tuple[str | None, str]:
    """(process name, window title) of the window in focus."""
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None, ""
    title = win32gui.GetWindowText(hwnd)
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    name = _process_name(pid)
    if name and name.lower() == "applicationframehost.exe":
        # Store (UWP) apps live in a child window owned by the real app process.
        children: list[int] = []

        def collect(child: int, _: Any) -> bool:
            children.append(child)
            return True

        with contextlib.suppress(pywintypes.error):
            win32gui.EnumChildWindows(hwnd, collect, None)
        for child in children:
            _, child_pid = win32process.GetWindowThreadProcessId(child)
            if child_pid != pid:
                name = _process_name(child_pid) or name
                break
    return name, title


# ---------- tray icon ----------


def _icon_image(color: str):  # noqa: ANN202 (PIL.Image)
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, 60, 60), fill=color, outline="white", width=4)
    draw.rectangle((29, 16, 35, 34), fill="white")  # clock hand
    draw.rectangle((29, 30, 46, 36), fill="white")
    return image


def describe(status: dict[str, Any] | None) -> tuple[str, str]:
    """(tooltip text, icon color) for the tray."""
    if not status or not status.get("known"):
        return f"{_TITLE}: нет связи со службой", "#808080"
    if not status.get("allowed"):
        return f"{_TITLE}: {status.get('reason') or 'время закончилось'}", "#c62828"
    remaining = int(status.get("remaining_minutes") or 0)
    hours, minutes = divmod(remaining, 60)
    left = f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"
    text = f"Осталось {left} (разрешено {status.get('window', '')})"
    if status.get("idle"):
        text += ", пауза: нет активности"
    color = "#2e7d32" if remaining > 15 else "#f9a825" if remaining > 5 else "#c62828"
    return text, color


def open_parent_window() -> None:
    """Start `pcagent parent` as a separate process (no console window)."""
    if getattr(sys, "frozen", False):
        command = [sys.executable, "parent"]
    else:
        command = [sys.executable, "-m", "pc_agent", "parent"]
    try:
        subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError:
        log.exception("Не удалось открыть окно родителя")


class Tray:
    def __init__(self) -> None:
        import pystray

        self._text, self._color = describe(None)
        self._icon = pystray.Icon(
            "ParentalControl",
            _icon_image(self._color),
            self._text,
            menu=pystray.Menu(
                pystray.MenuItem(lambda _item: self._text, None, enabled=False),
                # Default item: a click on the icon opens the parent window.
                pystray.MenuItem("Родитель...", lambda: open_parent_window(), default=True),
            ),
        )

    def update(self, status: dict[str, Any] | None) -> None:
        text, color = describe(status)
        if text != self._text:
            self._text = text
            self._icon.title = text[:127]  # Windows tooltip limit
            self._icon.update_menu()
        if color != self._color:
            self._color = color
            self._icon.icon = _icon_image(color)

    def run(self, worker: threading.Thread) -> None:
        def setup(icon) -> None:  # noqa: ANN001
            icon.visible = True
            worker.start()

        self._icon.run(setup=setup)

    def stop(self) -> None:
        self._icon.stop()


# ---------- main ----------


def _report_loop(stop: threading.Event, tray: Tray | None) -> None:
    client = PipeClient(PIPE_NAME)
    last_contact = time.monotonic()
    while not stop.is_set():
        try:
            app, title = foreground_window()
            report = {"type": "report", "idle_seconds": idle_seconds(), "app": app, "title": title}
        except Exception:
            log.exception("Не удалось определить активное окно")
            report = {"type": "report", "idle_seconds": idle_seconds(), "app": None, "title": ""}
        status = client.request(report)
        now = time.monotonic()
        if status is not None:
            last_contact = now
        elif now - last_contact > NO_CONTACT_EXIT_SECONDS:
            # The service restarted and started another helper, or it is gone.
            # Exit and free the session mutex so the service's new helper can run.
            log.info("Нет связи со службой, helper завершается")
            break
        if tray is not None:
            tray.update(status)
        stop.wait(REPORT_INTERVAL)
    client.close()
    if tray is not None:
        tray.stop()


def run_helper(tray: bool = True) -> int:
    mutex = win32event.CreateMutex(None, False, _MUTEX_NAME)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        return 0  # already running in this session
    stop = threading.Event()
    try:
        if not tray:
            _report_loop(stop, None)
            return 0
        icon = Tray()
        worker = threading.Thread(target=_report_loop, args=(stop, icon), daemon=True)
        icon.run(worker)  # blocks in the Windows message loop
        return 0
    finally:
        stop.set()
        mutex.Close()
