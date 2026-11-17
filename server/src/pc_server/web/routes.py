"""Web panel for the parent (server-rendered HTML, works on a phone)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from pc_server.api.deps import DbSession
from pc_server.services import devices
from pc_server.web import present
from pc_server.web.auth import Csrf, Parent, csrf_token, log_in, login_limiter
from pc_shared.rules import DayRules, Rules, Window

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals.update(
    csrf_token=csrf_token,
    minutes=present.minutes_text,
    local_time=present.local_time,
    event_text=present.event_text,
    event_label=lambda kind: present.EVENT_LABELS.get(kind, kind),
    alert_kinds=present.ALERT_KINDS,
    command_label=lambda action: present.COMMAND_LABELS.get(action, action),
    is_online=devices.is_online,
    relative_time=present.relative_time,
    meter=present.meter,
    bar_pct=present.bar_pct,
    event_icon=lambda kind: present.EVENT_ICONS.get(kind, "dot"),
)

router = APIRouter(include_in_schema=False)


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _flash(request: Request, text: str, ok: bool = True) -> None:
    request.session["flash"] = {"text": text, "ok": ok}


def _render(request: Request, name: str, **context) -> HTMLResponse:
    context["flash"] = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, context)


# ---------- login ----------


@router.get("/login")
async def login_page(request: Request, db: DbSession) -> HTMLResponse:
    has_password = await devices.get_parent_password(db) is not None
    return _render(request, "login.html", has_password=has_password, error=None)


@router.post("/login")
async def login(
    request: Request, db: DbSession, password: Annotated[str, Form()] = ""
) -> Response:
    has_password = await devices.get_parent_password(db) is not None
    locked = login_limiter.locked_for()
    if locked:
        error = f"Слишком много неверных паролей. Попробуйте через {int(locked // 60) + 1} мин."
    else:
        ok = await devices.check_parent_password(db, password)
        login_limiter.record(ok)
        if ok:
            log_in(request)
            return _redirect("/")
        error = "Неверный пароль."
    return templates.TemplateResponse(
        request, "login.html", {"has_password": has_password, "error": error, "flash": None},
        status_code=401,
    )


@router.post("/logout")
async def logout(request: Request, _csrf: Csrf) -> RedirectResponse:
    request.session.clear()
    return _redirect("/login")


# ---------- devices ----------


@router.get("/")
async def device_list(request: Request, db: DbSession, _parent: Parent) -> HTMLResponse:
    return _render(request, "devices.html", devices=await devices.list_devices(db))


@router.post("/devices/pair")
async def pair(request: Request, db: DbSession, _parent: Parent, _csrf: Csrf) -> HTMLResponse:
    code = await devices.create_pairing_code(db)
    server_url = str(request.base_url).rstrip("/")
    return _render(request, "pair.html", code=code, server_url=server_url)


@router.get("/devices/{device_id}")
async def device_page(
    request: Request, device_id: int, db: DbSession, _parent: Parent, kind: str = ""
) -> HTMLResponse:
    device = await devices.get_device(db, device_id)
    rules = Rules.model_validate(device.rules) if device.rules else Rules()
    status = device.status or {}
    today = date.today()
    return _render(
        request, "device.html",
        device=device, status=status, rules=rules,
        apps=await devices.app_usage(db, device, today),
        history=await devices.day_history(db, device),
        commands=await devices.recent_commands(db, device),
        events=await devices.recent_events(db, device, kind=kind or None),
        kind=kind, kinds=present.EVENT_LABELS,
    )


@router.get("/devices/{device_id}/status")
async def device_status_fragment(
    request: Request, device_id: int, db: DbSession, _parent: Parent
) -> HTMLResponse:
    """Refreshed by the device page every few seconds."""
    device = await devices.get_device(db, device_id)
    return templates.TemplateResponse(
        request, "_status.html", {"device": device, "status": device.status or {}}
    )


@router.post("/devices/{device_id}/command")
async def send_command(
    request: Request, device_id: int, db: DbSession, _parent: Parent, _csrf: Csrf,
    action: Annotated[str, Form()], minutes: Annotated[int | None, Form()] = None,
) -> RedirectResponse:
    device = await devices.get_device(db, device_id)
    try:
        await devices.create_command(db, device, action, minutes)
        text = f"{present.COMMAND_LABELS.get(action, action)}" + (
            f" {minutes:+d} мин" if action == "bonus" and minutes else "")
        online = devices.is_online(device)
        _flash(request, text + (": выполнится в течение нескольких секунд." if online
                                else ": выполнится, когда компьютер подключится."))
    except ValueError:
        _flash(request, "Неверная команда.", ok=False)
    return _redirect(f"/devices/{device_id}")


@router.post("/devices/{device_id}/rules")
async def save_rules(
    request: Request, device_id: int, db: DbSession, _parent: Parent, _csrf: Csrf,
    child_user: Annotated[str, Form()] = "",
    wd_limit: Annotated[int, Form()] = 120, wd_start: Annotated[str, Form()] = "08:00",
    wd_end: Annotated[str, Form()] = "21:00",
    we_limit: Annotated[int, Form()] = 180, we_start: Annotated[str, Form()] = "09:00",
    we_end: Annotated[str, Form()] = "22:00",
    warn: Annotated[str, Form()] = "10, 5, 1", idle_minutes: Annotated[int, Form()] = 3,
    enforcement: Annotated[str, Form()] = "logoff",
    blocked_apps: Annotated[str, Form()] = "",
) -> RedirectResponse:
    device = await devices.get_device(db, device_id)
    current = Rules.model_validate(device.rules) if device.rules else Rules()
    try:
        rules = Rules.model_validate({
            **current.model_dump(),  # keep fields the form does not show (blocked_sites, ...)
            "child_user": child_user,
            "weekdays": DayRules(limit_minutes=wd_limit, window=Window(start=wd_start, end=wd_end)),
            "weekends": DayRules(limit_minutes=we_limit, window=Window(start=we_start, end=we_end)),
            "warn_at_minutes": [int(x) for x in warn.replace(";", ",").split(",") if x.strip()],
            "idle_minutes": idle_minutes,
            "enforcement": enforcement,
            "blocked_apps": [line for line in blocked_apps.splitlines() if line.strip()],
        })
    except (ValidationError, ValueError) as exc:
        _flash(request, f"Правила не сохранены: {_first_error(exc)}", ok=False)
        return _redirect(f"/devices/{device_id}#rules")
    await devices.update_rules(db, device, rules)
    _flash(request, "Правила сохранены и будут отправлены на компьютер.")
    return _redirect(f"/devices/{device_id}#rules")


@router.post("/devices/{device_id}/rename")
async def rename(
    request: Request, device_id: int, db: DbSession, _parent: Parent, _csrf: Csrf,
    name: Annotated[str, Form()],
) -> RedirectResponse:
    await devices.rename_device(db, await devices.get_device(db, device_id), name)
    return _redirect(f"/devices/{device_id}")


@router.post("/devices/{device_id}/delete")
async def delete(
    request: Request, device_id: int, db: DbSession, _parent: Parent, _csrf: Csrf
) -> RedirectResponse:
    device = await devices.get_device(db, device_id)
    await devices.delete_device(db, device)
    _flash(request, f"Устройство «{device.name}» удалено. Агент на нём больше не подключится.")
    return _redirect("/")


def _first_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        err = exc.errors()[0]
        return str(err.get("msg", exc)).removeprefix("Value error, ")
    return "проверьте числа и время (ЧЧ:ММ)"
