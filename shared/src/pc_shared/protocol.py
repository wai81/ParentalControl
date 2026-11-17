"""Agent <-> server protocol (JSON over HTTPS).

The agent connects with a one-time pairing code from the web panel
(`RegisterRequest`) and gets a device token. Then, every few seconds, it sends
`SyncRequest` (status, new log entries, usage counters, results of commands)
and receives `SyncResponse` (newer rules, commands from the parent).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from pc_shared.rules import Rules

API_PREFIX = "/api/v1/agent"
DEFAULT_SYNC_INTERVAL = 15


class _Model(BaseModel):
    model_config = {"extra": "ignore"}  # newer peers may send more fields


class DeviceStatus(_Model):
    allowed: bool
    reason: str | None = None
    window: str = ""
    limit_minutes: float = 0
    used_minutes: float = 0
    remaining_minutes: float = 0
    bonus_minutes: int = 0
    blocked: bool = False
    child_logged_in: bool = False
    idle: bool = False
    app: str | None = None
    title: str | None = None
    agent_version: str = ""
    timezone: str = ""


class EventIn(_Model):
    local_id: int  # id in the agent's database: lets the server drop duplicates
    ts: datetime
    kind: str
    data: dict[str, Any] = Field(default_factory=dict)


class DayUsageIn(_Model):
    day: date
    used_minutes: float
    bonus_minutes: int = 0
    blocked: bool = False


class AppUsageIn(_Model):
    day: date
    app: str
    minutes: float


class CommandAck(_Model):
    id: int
    ok: bool
    message: str = ""


class Command(_Model):
    id: int
    action: Literal["bonus", "block", "unblock"]
    minutes: int | None = None


class RegisterRequest(_Model):
    code: str = Field(min_length=4, max_length=16)
    name: str = Field(min_length=1, max_length=100)
    rules: Rules | None = None  # the device's current rules become the server's copy


class RegisterResponse(_Model):
    device_id: int
    token: str


class SyncRequest(_Model):
    status: DeviceStatus | None = None
    events: list[EventIn] = Field(default_factory=list, max_length=1000)
    days: list[DayUsageIn] = Field(default_factory=list, max_length=10)
    apps: list[AppUsageIn] = Field(default_factory=list, max_length=500)
    acks: list[CommandAck] = Field(default_factory=list, max_length=100)
    rules_version: int = 0


class SyncResponse(_Model):
    rules: Rules | None = None  # only when newer than the agent's rules_version
    rules_version: int = 0
    commands: list[Command] = Field(default_factory=list)
    sync_interval: int = DEFAULT_SYNC_INTERVAL
