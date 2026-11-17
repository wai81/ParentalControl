"""System settings that close common ways around the agent.

Applied by `pcagent service install` and reverted by `pcagent service remove`.
All functions need administrator rights unless noted otherwise.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import winreg

import pywintypes
import win32net
import win32security

log = logging.getLogger(__name__)

USERS_SID = "S-1-5-32-545"
ADMINISTRATORS_SID = "S-1-5-32-544"
TIMEZONE_PRIVILEGE = "SeTimeZonePrivilege"

_SAFEBOOT_KEYS = (
    r"SYSTEM\CurrentControlSet\Control\SafeBoot\Minimal",
    r"SYSTEM\CurrentControlSet\Control\SafeBoot\Network",
)
_TZ_KEY = r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation"
OLD_TASK = r"\ParentalControl\Enforcer"


# ---------- time zone ----------


def current_timezone() -> str:
    """Windows time zone key name, e.g. "Belarus Standard Time". No admin rights needed."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _TZ_KEY) as key:
            return str(winreg.QueryValueEx(key, "TimeZoneKeyName")[0])
    except OSError:
        return ""


def _lsa_policy():  # noqa: ANN202 (PyLSA_HANDLE)
    return win32security.LsaOpenPolicy(None, win32security.POLICY_ALL_ACCESS)


def users_can_change_timezone() -> bool:
    policy = _lsa_policy()
    try:
        sid = win32security.ConvertStringSidToSid(USERS_SID)
        try:
            rights = win32security.LsaEnumerateAccountRights(policy, sid)
        except pywintypes.error:
            return False  # the group holds no rights at all
        return TIMEZONE_PRIVILEGE in rights
    finally:
        win32security.LsaClose(policy)


def set_users_can_change_timezone(allowed: bool) -> None:
    """Standard users (the child) may change the time zone by default in Windows.

    Moving to UTC+12 would start "tomorrow" early and reset the used time, so the
    right is taken away from the Users group. Administrators keep it. Takes effect
    for the child at the next sign-in.
    """
    policy = _lsa_policy()
    try:
        sid = win32security.ConvertStringSidToSid(USERS_SID)
        if allowed:
            win32security.LsaAddAccountRights(policy, sid, [TIMEZONE_PRIVILEGE])
        elif users_can_change_timezone():
            win32security.LsaRemoveAccountRights(policy, sid, False, [TIMEZONE_PRIVILEGE])
    finally:
        win32security.LsaClose(policy)


# ---------- safe mode ----------


def set_safe_mode_start(service_name: str, enabled: bool) -> None:
    """Let the service start in Safe Mode too (without this, Safe Mode = no control)."""
    for base in _SAFEBOOT_KEYS:
        path = rf"{base}\{service_name}"
        if enabled:
            hklm = winreg.HKEY_LOCAL_MACHINE
            with winreg.CreateKeyEx(hklm, path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "Service")
        else:
            with contextlib.suppress(FileNotFoundError):
                winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, path)


def safe_mode_start_enabled(service_name: str) -> bool:
    for base in _SAFEBOOT_KEYS:
        try:
            winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"{base}\{service_name}").Close()
        except FileNotFoundError:
            return False
    return True


# ---------- old PowerShell version ----------


def old_version_enabled() -> bool | None:
    """None if the PowerShell version is not installed, else whether its task is enabled.

    The task XML is used because the text output of schtasks depends on the Windows language.
    """
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", OLD_TASK, "/XML"], capture_output=True
    )
    if result.returncode != 0:
        return None
    xml = result.stdout.decode("utf-16", errors="ignore") if result.stdout[:2] in (
        b"\xff\xfe", b"\xfe\xff") else result.stdout.decode("utf-8", errors="ignore")
    return "<Enabled>false</Enabled>" not in xml.replace(" ", "")


def disable_old_version() -> None:
    subprocess.run(["schtasks", "/Change", "/TN", OLD_TASK, "/DISABLE"],
                   check=True, capture_output=True)


# ---------- child account ----------


def _sid_name(sid_string: str) -> str:
    name, _, _ = win32security.LookupAccountSid(
        None, win32security.ConvertStringSidToSid(sid_string)
    )
    return name


def check_child_account(user: str) -> list[str]:
    """Problems with the child's account that make control useless. No admin rights needed."""
    name = user.split("\\")[-1]
    try:
        win32net.NetUserGetInfo(None, name, 0)
    except pywintypes.error:
        return [
            f"Учётная запись '{name}' не найдена на этом компьютере. "
            "Нужно короткое имя пользователя (папка в C:\\Users), а не адрес почты."
        ]
    problems = []
    try:
        groups = {g.casefold() for g in win32net.NetUserGetLocalGroups(None, name, 0)}
        if _sid_name(ADMINISTRATORS_SID).casefold() in groups:
            problems.append(
                f"'{name}' - администратор компьютера. Ребёнок сможет отключить контроль. "
                "Сделайте учётную запись стандартной: Параметры > Учётные записи > "
                "Другие пользователи > Изменить тип учётной записи."
            )
    except pywintypes.error as exc:
        log.warning("Не удалось проверить группы пользователя %s: %s", name, exc)
    return problems
