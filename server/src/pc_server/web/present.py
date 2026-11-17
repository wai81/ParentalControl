"""Turning stored data into text for the web panel."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

EVENT_LABELS = {
    "agent_started": "Агент запущен",
    "agent_stopped": "Агент остановлен",
    "process_started": "Запущена программа",
    "process_stopped": "Закрыта программа",
    "app_blocked": "Закрыта запрещённая программа",
    "window_focus": "Окно",
    "warning": "Предупреждение ребёнку",
    "session_ended": "Сеанс завершён",
    "helper_started": "Помощник запущен",
    "helper_stopped": "Помощник был закрыт",
    "timezone_changed": "Изменён часовой пояс",
    "parent_command": "Команда родителя",
    "wrong_password": "Неверный пароль родителя",
    "rules_error": "Ошибка в правилах",
    "error": "Ошибка агента",
}
# Worth the parent's attention: shown highlighted.
ALERT_KINDS = {"app_blocked", "helper_stopped", "timezone_changed", "wrong_password",
               "rules_error", "error"}
COMMAND_LABELS = {"bonus": "Добавить время", "block": "Заблокировать", "unblock": "Разблокировать"}


EVENT_ICONS = {
    "agent_started": "power", "agent_stopped": "power",
    "process_started": "app", "process_stopped": "app",
    "app_blocked": "ban", "window_focus": "window",
    "warning": "clock", "session_ended": "logout",
    "helper_started": "shield", "helper_stopped": "alert",
    "timezone_changed": "globe", "parent_command": "user",
    "wrong_password": "key", "rules_error": "alert", "error": "alert",
}


def relative_time(value: datetime | None, now: datetime | None = None) -> str:
    """'только что', '5 мин назад', '3 ч назад', otherwise the date."""
    if value is None:
        return "никогда"
    now = now or datetime.now(UTC)
    seconds = (now - value).total_seconds()
    if seconds < 45:
        return "только что"
    if seconds < 3600:
        return f"{round(seconds / 60)} мин назад"
    if seconds < 24 * 3600:
        return f"{int(seconds // 3600)} ч назад"
    return local_time(value)


def meter(status: dict[str, Any]) -> dict[str, Any]:
    """Share of today's limit already used, and its status level (color + same words as text)."""
    limit = float(status.get("limit_minutes") or 0)
    used = float(status.get("used_minutes") or 0)
    pct = 100.0 if limit <= 0 else min(100.0, max(0.0, used / limit * 100))
    remaining = float(status.get("remaining_minutes") or 0)
    if not status.get("allowed", True) or remaining <= 5:
        level = "critical"
    elif remaining <= 15:
        level = "warning"
    else:
        level = "good"
    return {"pct": round(pct, 1), "level": level}


def bar_pct(value: float, largest: float) -> float:
    return 0.0 if largest <= 0 else round(max(0.0, value) / largest * 100, 1)


def minutes_text(minutes: float | None) -> str:
    minutes = max(0, round(minutes or 0))
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} ч {rest} мин"
    return f"{hours} ч" if hours else f"{rest} мин"


def local_time(value: datetime | None, fmt: str = "%d.%m %H:%M") -> str:
    return value.astimezone().strftime(fmt) if value else "—"


def event_text(kind: str, data: dict[str, Any]) -> str:
    if kind == "window_focus":
        seconds = int(data.get("seconds", 0))
        duration = minutes_text(seconds / 60) if seconds >= 60 else f"{seconds} с"
        return f"{data.get('app', '?')}: {data.get('title') or ''} ({duration})"
    if kind in ("process_started", "process_stopped"):
        return str(data.get("name", ""))
    if kind == "app_blocked":
        return f"{data.get('name', '')}" + ("" if data.get("killed", True) else " (не удалось)")
    if kind == "warning":
        if data.get("final"):
            return str(data.get("reason", ""))
        return f"осталось {data.get('remaining_minutes', '?')} мин"
    if kind == "session_ended":
        action = "выход из системы" if data.get("action") == "logoff" else "экран блокировки"
        return f"{action}: {data.get('reason', '')}"
    if kind == "timezone_changed":
        return f"{data.get('old', '?')} -> {data.get('new', '?')}"
    if kind == "parent_command":
        action = COMMAND_LABELS.get(data.get("action", ""), data.get("action", ""))
        minutes = data.get("minutes")
        return f"{action} {minutes:+d} мин" if minutes else action
    if kind in ("rules_error", "error"):
        return str(data.get("message", ""))[:200]
    if kind == "agent_started":
        return "режим наблюдения (без ограничений)" if data.get("dry_run") else ""
    if kind == "wrong_password":
        return "на компьютере ребёнка"
    if kind in ("agent_stopped", "helper_started", "helper_stopped"):
        return ""  # the label says it all; pids are of no use to the parent
    return ", ".join(f"{k}={v}" for k, v in data.items())
