"""Parent window: opened from the tray icon in the child's session.

The parent types the agent password and adds time or blocks the computer
without signing out the child. All checks happen in the service.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Send = Callable[[dict[str, Any]], dict[str, Any]]

TITLE = "Родительский контроль"
_MUTEX_NAME = "Local\\ParentalControlParentDialog"


def format_minutes(minutes: float) -> str:
    minutes = max(0, round(minutes))
    hours, rest = divmod(minutes, 60)
    return f"{hours} ч {rest} мин" if hours else f"{rest} мин"


def status_text(status: dict[str, Any]) -> str:
    if not status.get("ok", True) and "allowed" not in status:
        return status.get("message", "Служба не отвечает.")
    used = format_minutes(status.get("used_minutes", 0))
    limit = format_minutes(status.get("limit_minutes", 0))
    lines = []
    if status.get("allowed"):
        lines.append(f"Осталось {format_minutes(status.get('remaining_minutes', 0))}.")
    else:
        lines.append(status.get("reason") or "Сейчас пользоваться компьютером нельзя.")
    lines.append(f"Сегодня использовано {used} из {limit}. Разрешено {status.get('window', '')}.")
    if status.get("blocked"):
        lines.append("Компьютер заблокирован до конца дня.")
    return "\n".join(lines)


class ParentPanel:
    """Window logic without the GUI (tested directly)."""

    def __init__(self, send: Send) -> None:
        self._send = send
        self._password: str | None = None  # remembered after the first success

    def status(self) -> tuple[str, bool]:
        """(text, password is set)."""
        reply = self._send({"action": "status"})
        if "allowed" not in reply:
            return reply.get("message", "Служба не отвечает."), False
        if not reply.get("password_set"):
            return (
                status_text(reply) + "\n\nПароль родителя не задан. Задайте его от имени "
                "администратора: pcagent set-password",
                False,
            )
        return status_text(reply), True

    def run(self, action: str, password: str, minutes: int | None = None) -> tuple[bool, str]:
        password = password or self._password or ""
        if not password:
            return False, "Введите пароль родителя."
        message: dict[str, Any] = {"action": action, "password": password}
        if minutes is not None:
            message["minutes"] = minutes
        reply = self._send(message)
        if reply.get("ok"):
            self._password = password
        elif reply.get("error") == "wrong_password":
            self._password = None
        return bool(reply.get("ok")), str(reply.get("message", ""))


def run_dialog(send: Send | None = None) -> int:
    import tkinter as tk
    from tkinter import ttk

    import win32api
    import win32event
    import winerror

    mutex = win32event.CreateMutex(None, False, _MUTEX_NAME)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        return 0  # the window is already open
    if send is None:
        from pc_agent.pipe_client import send_command

        send = send_command
    panel = ParentPanel(send)

    root = tk.Tk()
    root.title(TITLE)
    root.resizable(False, False)
    root.attributes("-topmost", True)
    frame = ttk.Frame(root, padding=16)
    frame.grid()

    status_var = tk.StringVar()
    result_var = tk.StringVar()
    ttk.Label(frame, textvariable=status_var, justify="left", wraplength=380).grid(
        row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

    ttk.Label(frame, text="Пароль родителя:").grid(row=1, column=0, sticky="w")
    password = ttk.Entry(frame, show="●", width=28)
    password.grid(row=1, column=1, columnspan=2, sticky="we", pady=4)

    result = ttk.Label(frame, textvariable=result_var, wraplength=380, justify="left")
    buttons: list[ttk.Button] = []

    def refresh() -> None:
        text, has_password = panel.status()
        status_var.set(text)
        for button in buttons:
            button.state(["!disabled"] if has_password else ["disabled"])

    def do(action: str, minutes: int | None = None) -> None:
        ok, text = panel.run(action, password.get(), minutes)
        result_var.set(text)
        result.configure(foreground="#2e7d32" if ok else "#c62828")
        if ok:
            password.delete(0, tk.END)
        refresh()

    row = 2
    for col, (label, minutes) in enumerate((("+15 мин", 15), ("+30 мин", 30), ("+1 час", 60))):
        button = ttk.Button(frame, text=label, command=lambda m=minutes: do("bonus", m))
        button.grid(row=row, column=col, sticky="we", padx=2, pady=(8, 2))
        buttons.append(button)
    block = ttk.Button(frame, text="Заблокировать до конца дня", command=lambda: do("block"))
    block.grid(row=row + 1, column=0, columnspan=2, sticky="we", padx=2, pady=2)
    unblock = ttk.Button(frame, text="Снять блокировку", command=lambda: do("unblock"))
    unblock.grid(row=row + 1, column=2, sticky="we", padx=2, pady=2)
    buttons += [block, unblock]
    result.grid(row=row + 2, column=0, columnspan=3, sticky="w", pady=(10, 0))
    ttk.Button(frame, text="Закрыть", command=root.destroy).grid(
        row=row + 3, column=2, sticky="e", pady=(12, 0))

    refresh()
    password.focus_set()
    root.bind("<Escape>", lambda _e: root.destroy())
    try:
        root.mainloop()
    finally:
        mutex.Close()
    return 0
