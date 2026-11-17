from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select

from pc_server.models import Command, Device, Event, PairingCode
from pc_server.services import devices
from pc_shared.rules import DayRules, Rules

REGISTER = "/api/v1/agent/register"
SYNC = "/api/v1/agent/sync"


async def register(client, db_session, rules=None) -> tuple[int, dict]:
    code = await devices.create_pairing_code(db_session)
    body = {"code": code.code, "name": "Компьютер Маши"}
    if rules is not None:
        body["rules"] = rules.model_dump(mode="json")
    r = await client.post(REGISTER, json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    return data["device_id"], {"Authorization": f"Bearer {data['token']}"}


def status(**kw) -> dict:
    return {"allowed": True, "window": "08:00-21:00", "limit_minutes": 120,
            "used_minutes": 30, "remaining_minutes": 90, **kw}


async def test_register_with_code_once(client: AsyncClient, db_session):
    code = await devices.create_pairing_code(db_session)
    body = {"code": code.code, "name": "PC"}
    assert (await client.post(REGISTER, json=body)).status_code == 200
    again = await client.post(REGISTER, json=body)
    assert again.status_code == 400 and "Код" in again.json()["detail"]


async def test_expired_code_rejected(client: AsyncClient, db_session):
    db_session.add(PairingCode(code="12345678", expires_at=datetime.now(UTC) - timedelta(1)))
    await db_session.commit()
    r = await client.post(REGISTER, json={"code": "12345678", "name": "PC"})
    assert r.status_code == 400


async def test_token_stored_hashed(client: AsyncClient, db_session):
    device_id, headers = await register(client, db_session)
    device = await db_session.get(Device, device_id)
    token = headers["Authorization"].split()[1]
    assert device.token_hash != token and len(device.token_hash) == 64


async def test_sync_requires_valid_token(client: AsyncClient):
    assert (await client.post(SYNC, json={})).status_code == 401
    r = await client.post(SYNC, json={}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_sync_stores_status_events_usage(client: AsyncClient, db_session):
    device_id, headers = await register(client, db_session)
    ts = "2026-11-17T12:00:00+03:00"
    body = {
        "status": status(app="chrome.exe", child_logged_in=True),
        "events": [{"local_id": 1, "ts": ts, "kind": "app_blocked", "data": {"name": "roblox"}},
                   {"local_id": 2, "ts": ts, "kind": "warning", "data": {}}],
        "days": [{"day": "2026-11-17", "used_minutes": 30, "bonus_minutes": 15}],
        "apps": [{"day": "2026-11-17", "app": "chrome.exe", "minutes": 20}],
    }
    assert (await client.post(SYNC, json=body, headers=headers)).status_code == 200
    # Same events again (the agent did not get the answer): no duplicates.
    body["days"][0]["used_minutes"] = 35
    body["apps"][0]["minutes"] = 25
    assert (await client.post(SYNC, json=body, headers=headers)).status_code == 200

    device = await db_session.get(Device, device_id)
    await db_session.refresh(device)
    assert device.status["app"] == "chrome.exe" and device.last_seen_at is not None
    events = list(await db_session.scalars(select(Event).where(Event.device_id == device_id)))
    assert sorted(e.local_id for e in events) == [1, 2]
    assert events[0].ts.utcoffset() == timedelta(0)  # stored as UTC
    history = await devices.day_history(db_session, device)
    apps = await devices.app_usage(db_session, device, history[0].day)
    assert history[0].used_minutes == 35 and apps[0].minutes == 25


async def test_rules_from_registration_not_sent_back(client: AsyncClient, db_session):
    rules = Rules(child_user="masha", weekdays=DayRules(limit_minutes=90))
    device_id, headers = await register(client, db_session, rules)
    r = await client.post(SYNC, json={"rules_version": 1}, headers=headers)
    assert r.json()["rules"] is None and r.json()["rules_version"] == 1

    device = await db_session.get(Device, device_id)
    await devices.update_rules(db_session, device,
                               rules.model_copy(update={"blocked_apps": ["roblox"]}))
    r = await client.post(SYNC, json={"rules_version": 1}, headers=headers)
    assert r.json()["rules_version"] == 2
    assert r.json()["rules"]["blocked_apps"] == ["roblox"]
    assert r.json()["rules"]["weekdays"]["limit_minutes"] == 90


async def test_commands_redelivered_until_acked(client: AsyncClient, db_session):
    device_id, headers = await register(client, db_session)
    device = await db_session.get(Device, device_id)
    command = await devices.create_command(db_session, device, "bonus", 30)

    for _ in range(2):
        r = await client.post(SYNC, json={}, headers=headers)
        assert r.json()["commands"] == [{"id": command.id, "action": "bonus", "minutes": 30}]

    ack = {"acks": [{"id": command.id, "ok": True, "message": "Время изменено на +30 мин."}]}
    r = await client.post(SYNC, json=ack, headers=headers)
    assert r.json()["commands"] == []
    done = await db_session.scalar(select(Command).where(Command.id == command.id))
    await db_session.refresh(done)
    assert done.ok and done.done_at is not None and "+30" in done.result


async def test_cannot_ack_other_devices_command(client: AsyncClient, db_session):
    first_id, _ = await register(client, db_session)
    _, other_headers = await register(client, db_session)
    command = await devices.create_command(
        db_session, await db_session.get(Device, first_id), "block")
    await client.post(SYNC, json={"acks": [{"id": command.id, "ok": True}]},
                      headers=other_headers)
    await db_session.refresh(command)
    assert command.done_at is None


async def test_invalid_payload_rejected(client: AsyncClient, db_session):
    _, headers = await register(client, db_session)
    r = await client.post(SYNC, json={"events": [{"kind": "x"}]}, headers=headers)
    assert r.status_code == 422
