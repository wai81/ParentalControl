import re

from httpx import AsyncClient
from sqlalchemy import select

from pc_server.models import Command, Device
from pc_server.services import devices

PASSWORD = "parent-pass-1"


async def make_device(db_session, name="Компьютер Маши") -> Device:
    device = Device(name=name, token_hash=name.encode().hex()[:64].ljust(64, "0"))
    db_session.add(device)
    await db_session.commit()
    return device


async def login(client: AsyncClient, db_session) -> str:
    await devices.set_parent_password(db_session, PASSWORD)
    r = await client.post("/login", data={"password": PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/"
    page = await client.get("/")
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


async def test_pages_need_login(client: AsyncClient, db_session):
    device = await make_device(db_session)
    for url in ("/", f"/devices/{device.id}"):
        r = await client.get(url)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    assert (await client.get(f"/devices/{device.id}/status")).status_code == 401


async def test_login_page_without_password(client: AsyncClient):
    r = await client.get("/login")
    assert "pc_server set-password" in r.text


async def test_wrong_password_then_lockout(client: AsyncClient, db_session):
    await devices.set_parent_password(db_session, PASSWORD)
    for _ in range(3):
        r = await client.post("/login", data={"password": "guess"})
        assert r.status_code == 401 and "Неверный пароль" in r.text
    r = await client.post("/login", data={"password": PASSWORD})
    assert r.status_code == 401 and "Слишком много" in r.text


async def test_device_list_and_page(client: AsyncClient, db_session):
    device = await make_device(db_session)
    device.status = {"allowed": True, "remaining_minutes": 75, "used_minutes": 45,
                     "limit_minutes": 120, "window": "08:00-21:00", "child_logged_in": True,
                     "app": "minecraft.exe"}
    await db_session.commit()
    await login(client, db_session)
    page = await client.get("/")
    assert "Компьютер Маши" in page.text and "1 ч 15 мин" in page.text and "осталось" in page.text
    assert 'style="width: 37.5%"' in page.text  # meter: 45 of 120 minutes used
    page = await client.get(f"/devices/{device.id}")
    assert page.status_code == 200 and "minecraft.exe" in page.text


async def test_command_needs_csrf(client: AsyncClient, db_session):
    device = await make_device(db_session)
    csrf = await login(client, db_session)
    url = f"/devices/{device.id}/command"
    r = await client.post(url, data={"action": "bonus", "minutes": "30"})
    assert r.status_code == 400
    r = await client.post(url, data={"action": "bonus", "minutes": "30", "csrf": csrf})
    assert r.status_code == 303
    commands = list(await db_session.scalars(select(Command)))
    assert [(c.action, c.minutes) for c in commands] == [("bonus", 30)]
    page = await client.get(f"/devices/{device.id}")
    assert "ждёт подключения" in page.text


async def test_bad_command_rejected(client: AsyncClient, db_session):
    device = await make_device(db_session)
    csrf = await login(client, db_session)
    await client.post(f"/devices/{device.id}/command",
                      data={"action": "format", "csrf": csrf})
    assert list(await db_session.scalars(select(Command))) == []


async def test_save_rules(client: AsyncClient, db_session):
    device = await make_device(db_session)
    csrf = await login(client, db_session)
    form = {"csrf": csrf, "child_user": "masha", "wd_limit": "90", "wd_start": "8:00",
            "wd_end": "20:30", "we_limit": "150", "we_start": "10:00", "we_end": "22:00",
            "warn": "15, 5", "idle_minutes": "5", "enforcement": "lock",
            "blocked_apps": "Roblox*\nfortnite.exe\n"}
    r = await client.post(f"/devices/{device.id}/rules", data=form)
    assert r.status_code == 303
    await db_session.refresh(device)
    assert device.rules_version == 1
    assert device.rules["weekdays"] == {"limit_minutes": 90,
                                        "window": {"start": "08:00", "end": "20:30"}}
    assert device.rules["blocked_apps"] == ["roblox*", "fortnite"]
    assert device.rules["enforcement"] == "lock"

    bad = {**form, "wd_end": "25:00"}
    await client.post(f"/devices/{device.id}/rules", data=bad)
    await db_session.refresh(device)
    assert device.rules_version == 1  # invalid rules are not saved
    page = await client.get(f"/devices/{device.id}")
    assert "Правила не сохранены" in page.text


async def test_pairing_page_shows_code(client: AsyncClient, db_session):
    csrf = await login(client, db_session)
    r = await client.post("/devices/pair", data={"csrf": csrf})
    code = re.search(r'class="code[^"]*">(\d{8})<', r.text).group(1)
    assert f"--code {code}" in r.text and "--server http://test" in r.text


async def test_delete_device(client: AsyncClient, db_session):
    device = await make_device(db_session)
    csrf = await login(client, db_session)
    r = await client.post(f"/devices/{device.id}/delete", data={"csrf": csrf})
    assert r.status_code == 303
    assert await db_session.get(Device, device.id) is None


def test_meter_levels_and_relative_time():
    from datetime import UTC, datetime, timedelta

    from pc_server.web.present import meter, relative_time

    assert meter({"allowed": True, "limit_minutes": 120, "used_minutes": 30,
                  "remaining_minutes": 90}) == {"pct": 25.0, "level": "good"}
    assert meter({"allowed": True, "limit_minutes": 60, "used_minutes": 50,
                  "remaining_minutes": 10})["level"] == "warning"
    assert meter({"allowed": False, "limit_minutes": 0, "used_minutes": 5})["pct"] == 100
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    assert relative_time(now - timedelta(seconds=10), now) == "только что"
    assert relative_time(now - timedelta(minutes=5), now) == "5 мин назад"
    assert relative_time(now - timedelta(hours=3), now) == "3 ч назад"
    assert relative_time(None) == "никогда"
