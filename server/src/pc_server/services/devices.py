"""Business logic shared by the agent API and the web panel."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from pc_server.core.config import settings
from pc_server.core.exceptions import AuthError, InvalidPairingCode, NotFoundError
from pc_server.core.security import new_device_token, new_pairing_code, token_hash
from pc_server.models import AppUsage, Command, DayUsage, Device, Event, PairingCode, Setting
from pc_shared.passwords import hash_password, verify_password
from pc_shared.protocol import (
    Command as CommandOut,
)
from pc_shared.protocol import (
    RegisterRequest,
    SyncRequest,
    SyncResponse,
)
from pc_shared.rules import Rules

PARENT_PASSWORD_KEY = "parent_password"
COMMAND_ACTIONS = ("bonus", "block", "unblock")


def _now() -> datetime:
    return datetime.now(UTC)


# ---------- parent password ----------


async def get_parent_password(db: AsyncSession) -> dict | None:
    row = await db.get(Setting, PARENT_PASSWORD_KEY)
    return json.loads(row.value) if row else None


async def set_parent_password(db: AsyncSession, password: str) -> None:
    value = json.dumps(hash_password(password))
    row = await db.get(Setting, PARENT_PASSWORD_KEY)
    if row:
        row.value = value
    else:
        db.add(Setting(key=PARENT_PASSWORD_KEY, value=value))
    await db.commit()


async def check_parent_password(db: AsyncSession, password: str) -> bool:
    record = await get_parent_password(db)
    return bool(record) and verify_password(password, record)


# ---------- pairing ----------


async def create_pairing_code(db: AsyncSession) -> PairingCode:
    await db.execute(delete(PairingCode).where(PairingCode.expires_at < _now()))
    code = PairingCode(
        code=new_pairing_code(),
        expires_at=_now() + timedelta(minutes=settings.pairing_code_minutes),
    )
    db.add(code)
    await db.commit()
    return code


async def register_device(db: AsyncSession, request: RegisterRequest) -> tuple[Device, str]:
    pairing = await db.get(PairingCode, request.code.strip())
    if pairing is None or pairing.expires_at < _now():
        raise InvalidPairingCode()
    await db.delete(pairing)  # one-time
    token = new_device_token()
    device = Device(
        name=request.name.strip(),
        token_hash=token_hash(token),
        rules=request.rules.model_dump(mode="json") if request.rules else None,
        rules_version=1 if request.rules else 0,
    )
    db.add(device)
    await db.commit()
    return device, token


async def authenticate_device(db: AsyncSession, token: str) -> Device:
    device = await db.scalar(select(Device).where(Device.token_hash == token_hash(token)))
    if device is None:
        raise AuthError("Неизвестное устройство.")
    return device


# ---------- sync ----------


async def sync_device(db: AsyncSession, device: Device, request: SyncRequest) -> SyncResponse:
    device.last_seen_at = _now()
    if request.status is not None:
        device.status = request.status.model_dump(mode="json")

    if request.events:
        ids = [e.local_id for e in request.events]
        known = set(
            await db.scalars(
                select(Event.local_id).where(Event.device_id == device.id, Event.local_id.in_(ids))
            )
        )
        for e in request.events:
            if e.local_id not in known:
                known.add(e.local_id)
                db.add(Event(device_id=device.id, local_id=e.local_id, ts=e.ts,
                             kind=e.kind[:32], data=e.data))

    for d in request.days:
        row = await db.get(DayUsage, (device.id, d.day))
        if row is None:
            row = DayUsage(device_id=device.id, day=d.day)
            db.add(row)
        row.used_minutes = d.used_minutes
        row.bonus_minutes = d.bonus_minutes
        row.blocked = d.blocked

    for a in request.apps:
        row = await db.get(AppUsage, (device.id, a.day, a.app[:100]))
        if row is None:
            db.add(AppUsage(device_id=device.id, day=a.day, app=a.app[:100], minutes=a.minutes))
        else:
            row.minutes = a.minutes

    for ack in request.acks:
        command = await db.get(Command, ack.id)
        if command is not None and command.device_id == device.id and command.done_at is None:
            command.done_at, command.ok, command.result = _now(), ack.ok, ack.message[:500]

    # Pending commands are sent again until the agent confirms them (it skips repeats).
    pending = list(
        await db.scalars(
            select(Command)
            .where(Command.device_id == device.id, Command.done_at.is_(None))
            .order_by(Command.id)
        )
    )
    for command in pending:
        command.delivered_at = command.delivered_at or _now()

    response = SyncResponse(
        rules_version=device.rules_version,
        commands=[CommandOut(id=c.id, action=c.action, minutes=c.minutes) for c in pending],
    )
    if device.rules is not None and device.rules_version > request.rules_version:
        response.rules = Rules.model_validate(device.rules)
    await db.commit()
    return response


# ---------- web panel ----------


async def list_devices(db: AsyncSession) -> list[Device]:
    return list(await db.scalars(select(Device).order_by(Device.name)))


async def get_device(db: AsyncSession, device_id: int) -> Device:
    device = await db.get(Device, device_id)
    if device is None:
        raise NotFoundError("Устройство не найдено.")
    return device


def is_online(device: Device, now: datetime | None = None) -> bool:
    if device.last_seen_at is None:
        return False
    now = now or _now()
    return (now - device.last_seen_at).total_seconds() <= settings.device_online_seconds


async def create_command(
    db: AsyncSession, device: Device, action: str, minutes: int | None = None
) -> Command:
    if action not in COMMAND_ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    if action == "bonus" and (minutes is None or minutes == 0 or abs(minutes) > 600):
        raise ValueError("minutes must be 1..600")
    command = Command(device_id=device.id, action=action,
                      minutes=minutes if action == "bonus" else None)
    db.add(command)
    await db.commit()
    return command


async def update_rules(db: AsyncSession, device: Device, rules: Rules) -> None:
    device.rules = rules.model_dump(mode="json")
    device.rules_version += 1
    await db.commit()


async def rename_device(db: AsyncSession, device: Device, name: str) -> None:
    device.name = name.strip()[:100] or device.name
    await db.commit()


async def delete_device(db: AsyncSession, device: Device) -> None:
    for model in (Command, Event, DayUsage, AppUsage):
        await db.execute(delete(model).where(model.device_id == device.id))
    await db.delete(device)
    await db.commit()


async def recent_commands(db: AsyncSession, device: Device, limit: int = 10) -> list[Command]:
    return list(await db.scalars(
        select(Command).where(Command.device_id == device.id)
        .order_by(Command.id.desc()).limit(limit)
    ))


async def recent_events(
    db: AsyncSession, device: Device, limit: int = 200, kind: str | None = None
) -> list[Event]:
    query = select(Event).where(Event.device_id == device.id)
    if kind:
        query = query.where(Event.kind == kind)
    return list(await db.scalars(query.order_by(Event.ts.desc()).limit(limit)))


async def app_usage(db: AsyncSession, device: Device, day: date) -> list[AppUsage]:
    return list(await db.scalars(
        select(AppUsage).where(AppUsage.device_id == device.id, AppUsage.day == day)
        .order_by(AppUsage.minutes.desc()).limit(30)
    ))


async def day_history(db: AsyncSession, device: Device, days: int = 14) -> list[DayUsage]:
    return list(await db.scalars(
        select(DayUsage).where(DayUsage.device_id == device.id)
        .order_by(DayUsage.day.desc()).limit(days)
    ))
