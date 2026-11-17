"""Pure decision logic: is the child allowed to use the computer right now?"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from pc_shared.rules import Rules


@dataclass(frozen=True)
class DayState:
    """Counters for one calendar day."""

    day: date
    used_seconds: float = 0.0
    bonus_minutes: int = 0
    blocked: bool = False
    warned: frozenset[int] = frozenset()

    @property
    def used_minutes(self) -> float:
        return self.used_seconds / 60


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None
    window: str
    limit_minutes: float
    used_minutes: float
    left_by_limit: float
    remaining_minutes: float
    # Warning thresholds crossed just now (show a message) and the full set to remember.
    warn_now: frozenset[int]
    warned_after: frozenset[int]


def evaluate(rules: Rules, state: DayState, now: datetime) -> Decision:
    day_rules = rules.for_date(now.date())
    window = day_rules.window
    minute = now.hour * 60 + now.minute + now.second / 60

    in_window = window.start_minute <= minute < window.end_minute
    limit = float(day_rules.limit_minutes + state.bonus_minutes)
    left_by_limit = limit - state.used_minutes
    left_by_window = window.end_minute - minute if in_window else 0.0
    if in_window and window.end_minute == 24 * 60:
        # "until 24:00" followed by "from 00:00" tomorrow: midnight is not a deadline,
        # so no "10 minutes left" warning at 23:50.
        tomorrow = rules.for_date(now.date() + timedelta(days=1))
        if tomorrow.window.start_minute == 0:
            left_by_window = float("inf")
    remaining = max(0.0, min(left_by_limit, left_by_window))

    reason: str | None = None
    if state.blocked:
        reason = "Родители заблокировали компьютер до конца дня."
    elif not in_window:
        reason = f"Сейчас пользоваться компьютером нельзя. Разрешённое время: {window}."
    elif left_by_limit <= 0:
        reason = "Время за компьютером на сегодня закончилось."

    allowed = reason is None
    if allowed:
        # Thresholds re-arm automatically when bonus time pushes "remaining" back up.
        warned_after = frozenset(w for w in rules.warn_at_minutes if remaining <= w)
        warn_now = warned_after - state.warned
    else:
        warned_after = state.warned
        warn_now = frozenset()

    return Decision(
        allowed=allowed,
        reason=reason,
        window=str(window),
        limit_minutes=limit,
        used_minutes=state.used_minutes,
        left_by_limit=left_by_limit,
        remaining_minutes=remaining,
        warn_now=warn_now,
        warned_after=warned_after,
    )
