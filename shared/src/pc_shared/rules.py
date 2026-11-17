"""Rules that the parent sets for a child.

The same model is used by the agent (read from rules.json or received from the
server) and by the server (stored and edited in the web panel).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_HHMM = re.compile(r"(\d{1,2}):(\d{2})")


def parse_hhmm(text: str) -> int:
    """Convert "HH:MM" to minutes since midnight. "24:00" is allowed as the end of the day."""
    m = _HHMM.fullmatch(text.strip())
    if not m:
        raise ValueError(f"неверный формат времени {text!r}, нужно ЧЧ:ММ")
    hours, minutes = int(m[1]), int(m[2])
    if minutes >= 60 or hours > 24 or (hours == 24 and minutes):
        raise ValueError(f"неверное время {text!r}")
    return hours * 60 + minutes


def format_minutes(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class Window(BaseModel):
    """Hours of the day when the computer may be used, [start, end)."""

    model_config = ConfigDict(frozen=True)

    start: str = "00:00"
    end: str = "24:00"

    @field_validator("start", "end")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return format_minutes(parse_hhmm(value))

    @model_validator(mode="after")
    def _check_order(self) -> Window:
        if self.start_minute >= self.end_minute:
            raise ValueError(f"начало {self.start} должно быть раньше конца {self.end}")
        return self

    @property
    def start_minute(self) -> int:
        return parse_hhmm(self.start)

    @property
    def end_minute(self) -> int:
        return parse_hhmm(self.end)

    def __str__(self) -> str:
        return f"{self.start}-{self.end}"


class DayRules(BaseModel):
    model_config = ConfigDict(frozen=True)

    limit_minutes: int = Field(ge=0, le=24 * 60)
    window: Window = Window()


class Rules(BaseModel):
    """All rules for one child. Unknown fields are ignored for forward compatibility."""

    child_user: str = ""
    weekdays: DayRules = DayRules(limit_minutes=120, window=Window(start="08:00", end="21:00"))
    weekends: DayRules = DayRules(limit_minutes=180, window=Window(start="09:00", end="22:00"))
    warn_at_minutes: list[int] = Field(default_factory=lambda: [10, 5, 1])
    # No mouse/keyboard input for this long: time is not counted. 0 disables the check.
    idle_minutes: int = Field(default=3, ge=0, le=60)
    # What to do when time is over: end the session or only return to the lock screen.
    enforcement: Literal["logoff", "lock"] = "logoff"
    # Process names, wildcards allowed: "RobloxPlayerBeta", "Minecraft*". ".exe" is optional.
    blocked_apps: list[str] = Field(default_factory=list)
    blocked_sites: list[str] = Field(default_factory=list)

    @field_validator("child_user")
    @classmethod
    def _strip_user(cls, value: str) -> str:
        return value.strip()

    @field_validator("warn_at_minutes")
    @classmethod
    def _normalize_warnings(cls, value: list[int]) -> list[int]:
        if any(v <= 0 for v in value):
            raise ValueError("минуты предупреждений должны быть больше нуля")
        return sorted(set(value), reverse=True)

    @field_validator("blocked_apps")
    @classmethod
    def _normalize_apps(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for item in value:
            name = item.strip().lower().removesuffix(".exe")
            if name and name not in result:
                result.append(name)
        return result

    def for_date(self, day: date) -> DayRules:
        return self.weekends if day.weekday() >= 5 else self.weekdays
