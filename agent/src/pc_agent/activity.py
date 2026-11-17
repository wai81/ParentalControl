"""Data exchanged between the service and the helper in the child's session.

The pipe server thread pushes reports from the helper into `ActivityHub`; the
engine reads them on each tick and publishes a status the helper shows in the
tray. Pure Python, no Windows calls: easy to test.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any

import psutil

PIPE_NAME = r"\\.\pipe\ParentalControlAgent"
REPORT_INTERVAL = 2.0
# Reports older than this are ignored: the helper is gone or hung.
MAX_REPORT_AGE = 10.0


@dataclass(frozen=True)
class Activity:
    idle_seconds: float
    app: str | None
    title: str
    received: float  # time.monotonic() when the service got it


def encode(message: dict[str, Any]) -> bytes:
    return json.dumps(message, ensure_ascii=False).encode("utf-8")


def decode(data: bytes) -> dict[str, Any]:
    message = json.loads(data.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("message must be a JSON object")
    return message


def parse_report(message: dict[str, Any], received: float) -> Activity:
    if message.get("type") != "report":
        raise ValueError(f"unexpected message type {message.get('type')!r}")
    idle = float(message.get("idle_seconds", 0.0))
    app = message.get("app") or None
    title = str(message.get("title") or "")[:300]
    return Activity(
        idle_seconds=max(idle, 0.0),
        app=str(app)[:100] if app else None,
        title=title,
        received=received,
    )


class ActivityHub:
    """Thread-safe meeting point of the pipe server thread and the engine."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._activity: Activity | None = None
        self._status: dict[str, Any] = {"type": "status", "known": False}
        self._expected_pid: int | None = None

    def set_expected_pid(self, pid: int | None) -> None:
        with self._lock:
            self._expected_pid = pid
            self._activity = None

    def accepts(self, client_pid: int) -> bool:
        """Only the helper the service started (or its direct child, e.g. under a
        venv launcher) may report activity."""
        with self._lock:
            expected = self._expected_pid
        if expected is None:
            return False
        if client_pid == expected:
            return True
        try:
            return psutil.Process(client_pid).ppid() == expected
        except psutil.Error:
            return False

    def push(self, activity: Activity) -> None:
        with self._lock:
            self._activity = activity

    def latest(self, mono: float | None = None) -> Activity | None:
        mono = time.monotonic() if mono is None else mono
        with self._lock:
            activity = self._activity
        if activity is None or mono - activity.received > MAX_REPORT_AGE:
            return None
        return activity

    def set_status(self, status: dict[str, Any]) -> None:
        with self._lock:
            self._status = {"type": "status", "known": True, **status}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)
