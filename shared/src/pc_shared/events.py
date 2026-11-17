"""Activity events recorded by the agent and later sent to the server."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventKind(StrEnum):
    AGENT_STARTED = "agent_started"
    AGENT_STOPPED = "agent_stopped"
    PROCESS_STARTED = "process_started"
    PROCESS_STOPPED = "process_stopped"
    APP_BLOCKED = "app_blocked"
    WINDOW_FOCUS = "window_focus"
    HELPER_STARTED = "helper_started"
    HELPER_STOPPED = "helper_stopped"
    TIMEZONE_CHANGED = "timezone_changed"
    WARNING = "warning"
    SESSION_ENDED = "session_ended"
    RULES_ERROR = "rules_error"
    ERROR = "error"


def local_now() -> datetime:
    return datetime.now().astimezone()


class Event(BaseModel):
    ts: datetime = Field(default_factory=local_now)
    kind: EventKind
    data: dict[str, Any] = Field(default_factory=dict)
