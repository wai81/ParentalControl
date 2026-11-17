"""Parent commands received by the service: add time, block, unblock, status.

Anyone in the child's session can connect to the control pipe, so every
command except "status" needs the parent's password. The service applies the
change itself (as SYSTEM), which is how the child's own account can be used to
run them without the Windows administrator password.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from pc_agent.policy import evaluate
from pc_agent.rules_file import load_rules
from pc_agent.secret import AttemptLimiter, load_record, verify_password
from pc_agent.storage import Store
from pc_shared.events import Event, EventKind, local_now
from pc_shared.rules import Rules

log = logging.getLogger(__name__)

CONTROL_PIPE_NAME = r"\\.\pipe\ParentalControlAgent.control"
MAX_BONUS_MINUTES = 600
NO_PASSWORD_MESSAGE = (
    "Пароль родителя не задан. Задайте его от имени администратора: pcagent set-password"
)


class ControlHandler:
    def __init__(self, data_dir: Path, limiter: AttemptLimiter | None = None) -> None:
        self._data_dir = data_dir
        self._limiter = limiter or AttemptLimiter()

    def handle(
        self, message: dict[str, Any], now: datetime | None = None, mono: float | None = None
    ) -> dict[str, Any]:
        now = now or local_now()
        mono = time.monotonic() if mono is None else mono
        action = message.get("action")
        if action == "status":
            return {"ok": True, **self._status(now)}
        if action not in ("check", "bonus", "block", "unblock"):
            return {"ok": False, "error": "bad_request", "message": "Неизвестная команда."}

        record = load_record(self._data_dir)
        if record is None:
            return {"ok": False, "error": "no_password", "message": NO_PASSWORD_MESSAGE}
        locked = self._limiter.locked_for(mono)
        if locked:
            minutes = math.ceil(locked / 60)
            return {
                "ok": False, "error": "locked",
                "message": f"Слишком много неверных паролей. Попробуйте через {minutes} мин.",
            }

        ok = verify_password(str(message.get("password", "")), record)
        self._limiter.record(ok, mono)
        with Store(self._data_dir / "agent.db") as store:
            if not ok:
                log.warning("Неверный пароль родителя (команда %s)", action)
                store.add_event(Event(kind=EventKind.WRONG_PASSWORD, data={"action": action}))
                return {"ok": False, "error": "wrong_password", "message": "Неверный пароль."}
            result = self._apply(store, action, message, now)
        if result["ok"]:
            result.update(self._status(now))
        return result

    def _apply(
        self, store: Store, action: str, message: dict[str, Any], now: datetime
    ) -> dict[str, Any]:
        if action == "check":
            return {"ok": True, "message": "Пароль верный."}
        return apply_command(store, action, message.get("minutes"), now, source="pc")

    def _status(self, now: datetime) -> dict[str, Any]:
        status = compute_status(self._data_dir, now)
        status["password_set"] = load_record(self._data_dir) is not None
        return status


def apply_command(
    store: Store, action: str, minutes: Any, now: datetime, source: str
) -> dict[str, Any]:
    """Bonus/block/unblock for today. `source`: "pc" (parent window) or "server"."""
    today = now.date()
    data: dict[str, Any] = {}
    if action == "bonus":
        try:
            minutes = int(minutes or 0)
        except (TypeError, ValueError):
            minutes = 0
        if minutes == 0 or abs(minutes) > MAX_BONUS_MINUTES:
            return {
                "ok": False, "error": "bad_request",
                "message": f"Укажите от 1 до {MAX_BONUS_MINUTES} минут.",
            }
        store.add_bonus(today, minutes)
        text = f"Время на сегодня изменено на {minutes:+d} мин."
        data["minutes"] = minutes
    elif action == "block":
        store.set_blocked(today, True)
        text = "Компьютер заблокирован до конца дня."
    elif action == "unblock":
        store.set_blocked(today, False)
        text = "Блокировка на сегодня снята."
    else:
        return {"ok": False, "error": "bad_request", "message": "Неизвестная команда."}
    log.info("Команда родителя (%s): %s %s", source, action, data or "")
    store.add_event(Event(kind=EventKind.PARENT_COMMAND,
                          data={"action": action, "source": source, **data}))
    return {"ok": True, "message": text}


def compute_status(data_dir: Path, now: datetime) -> dict[str, Any]:
    """Time left etc. from rules.json and the database (no password needed)."""
    rules_path = data_dir / "rules.json"
    try:
        rules = load_rules(rules_path) if rules_path.exists() else Rules()
    except (ValueError, OSError):
        rules = Rules()
    with Store(data_dir / "agent.db") as store:
        state = store.load_day(now.date())
    d = evaluate(rules, state, now)
    return {
        "allowed": d.allowed,
        "reason": d.reason,
        "window": d.window,
        "limit_minutes": d.limit_minutes,
        "used_minutes": round(d.used_minutes, 1),
        "remaining_minutes": round(d.remaining_minutes, 1),
        "bonus_minutes": state.bonus_minutes,
        "blocked": state.blocked,
    }
