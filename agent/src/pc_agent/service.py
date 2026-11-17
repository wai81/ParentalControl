"""Windows service: runs the agent as SYSTEM and starts it with Windows.

Works with the packaged pcagent.exe (PyInstaller, see scripts/build-agent.ps1).
`pcagent service install` copies the build to Program Files (writable only by
administrators), registers the service with automatic restart and starts it.
From a source checkout use `pcagent run` instead.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pywintypes
import servicemanager
import win32service
import win32serviceutil

from pc_agent.runner import Paths, default_data_dir, run_agent

SERVICE_NAME = "ParentalControlAgent"
DISPLAY_NAME = "Parental Control Agent"
DESCRIPTION = "Родительский контроль: учёт времени, ограничения и журнал активности."
CLASS_STRING = "pc_agent.service.AgentService"

_ERROR_SERVICE_DOES_NOT_EXIST = 1060
_ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063

_STATE_NAMES = {
    win32service.SERVICE_STOPPED: "остановлена",
    win32service.SERVICE_START_PENDING: "запускается",
    win32service.SERVICE_STOP_PENDING: "останавливается",
    win32service.SERVICE_RUNNING: "работает",
    win32service.SERVICE_PAUSED: "приостановлена",
}


class ServiceError(Exception):
    """A problem the user can fix; printed without a traceback."""


def install_dir() -> Path:
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    return Path(program_files) / "ParentalControl" / "agent"


class AgentService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = DISPLAY_NAME
    _svc_description_ = DESCRIPTION

    def __init__(self, args: list[str]) -> None:
        super().__init__(args)
        self._stop = threading.Event()

    def SvcStop(self) -> None:  # noqa: N802 (pywin32 API name)
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self._stop.set()

    def SvcDoRun(self) -> None:  # noqa: N802 (pywin32 API name)
        servicemanager.LogInfoMsg(f"{SERVICE_NAME} started")
        try:
            run_agent(Paths(default_data_dir()), stop=self._stop)
        except Exception as exc:
            servicemanager.LogErrorMsg(f"{SERVICE_NAME} crashed: {exc!r}")
            raise


def run_dispatcher() -> bool:
    """Hand control to the Service Control Manager.

    Returns False when the exe was started by a person (e.g. double-click),
    not by the SCM: then the caller shows the command line help instead.
    """
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(AgentService)
    try:
        servicemanager.StartServiceCtrlDispatcher()
    except pywintypes.error as exc:
        if exc.winerror == _ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
            return False
        raise
    return True


# ---------- helpers ----------


def _require_admin() -> None:
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise ServiceError(
            "Нужны права администратора: откройте терминал через "
            "«Запуск от имени администратора»."
        )


def _require_frozen() -> None:
    if not getattr(sys, "frozen", False):
        raise ServiceError(
            "Служба устанавливается из собранного pcagent.exe "
            "(scripts/build-agent.ps1). Из исходников используйте 'pcagent run'."
        )


def _state() -> int | None:
    try:
        return win32serviceutil.QueryServiceStatus(SERVICE_NAME)[1]
    except pywintypes.error as exc:
        if exc.winerror == _ERROR_SERVICE_DOES_NOT_EXIST:
            return None
        raise


def _stop_if_running() -> None:
    if _state() not in (None, win32service.SERVICE_STOPPED):
        win32serviceutil.StopService(SERVICE_NAME)
        win32serviceutil.WaitForServiceStatus(SERVICE_NAME, win32service.SERVICE_STOPPED, 30)


def _start() -> None:
    win32serviceutil.StartService(SERVICE_NAME)
    win32serviceutil.WaitForServiceStatus(SERVICE_NAME, win32service.SERVICE_RUNNING, 30)


def _sc(*args: str) -> None:
    result = subprocess.run(["sc.exe", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise ServiceError(f"sc {' '.join(args)}: {result.stdout.strip() or result.stderr.strip()}")


def harden_data_dir(paths: Paths) -> None:
    """Only SYSTEM and Administrators may change agent data; users may only read it.

    Without this a standard user could create rules.json in ProgramData before
    the parent does: ProgramData lets users create new files by default.
    """
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "icacls", str(paths.data_dir), "/inheritance:r",
            "/grant:r", "*S-1-5-18:(OI)(CI)F",       # SYSTEM
            "/grant:r", "*S-1-5-32-544:(OI)(CI)F",   # Administrators
            "/grant:r", "*S-1-5-32-545:(OI)(CI)RX",  # Users: read only
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ServiceError(f"icacls: {result.stdout.strip() or result.stderr.strip()}")


def _copy_build(target: Path) -> Path:
    """Copy the one-folder build next to sys.executable into `target`."""
    source = Path(sys.executable).resolve().parent
    if source == target.resolve():
        return target / Path(sys.executable).name
    if target.exists():
        if not (target / "pcagent.exe").exists():
            raise ServiceError(f"{target} существует, но это не папка агента; удалите её вручную.")
        shutil.rmtree(target)
    shutil.copytree(source, target)
    return target / Path(sys.executable).name


# ---------- commands ----------


def install() -> None:
    """Install or upgrade: copy files, protect data, register, start."""
    _require_frozen()
    _require_admin()
    paths = Paths(default_data_dir())
    existed = _state() is not None
    _stop_if_running()

    exe = _copy_build(install_dir())
    print(f"Файлы агента: {exe.parent}")
    harden_data_dir(paths)
    print(f"Данные агента (только чтение для пользователей): {paths.data_dir}")

    if existed:
        win32serviceutil.ChangeServiceConfig(
            CLASS_STRING, SERVICE_NAME,
            startType=win32service.SERVICE_AUTO_START,
            exeName=str(exe), displayName=DISPLAY_NAME, description=DESCRIPTION,
        )
    else:
        win32serviceutil.InstallService(
            CLASS_STRING, SERVICE_NAME, DISPLAY_NAME,
            startType=win32service.SERVICE_AUTO_START,
            exeName=str(exe), description=DESCRIPTION,
        )
    # Restart 5 s after a crash or kill, and also after an exit with an error code.
    _sc("failure", SERVICE_NAME, "reset=", "86400",
        "actions=", "restart/5000/restart/5000/restart/5000")
    _sc("failureflag", SERVICE_NAME, "1")

    _start()
    print(f"Служба {SERVICE_NAME} {'обновлена' if existed else 'установлена'} и запущена.")
    if not paths.rules.exists():
        print(
            f"Правил пока нет, агент ничего не ограничивает. Создайте их:\n"
            f'  "{exe}" init --child ИМЯ_РЕБЁНКА'
        )


def remove() -> None:
    _require_admin()
    if _state() is None:
        print("Служба не установлена.")
        return
    _stop_if_running()
    win32serviceutil.RemoveService(SERVICE_NAME)
    print(f"Служба {SERVICE_NAME} удалена.")
    print(f"Файлы остались в {install_dir()} и {default_data_dir()}, их можно удалить вручную.")


def control(action: str) -> None:
    if action == "status":
        state = _state()
        print("Служба не установлена." if state is None
              else f"Служба {SERVICE_NAME}: {_STATE_NAMES.get(state, state)}")
        return
    _require_admin()
    if _state() is None:
        raise ServiceError("Служба не установлена: pcagent service install")
    if action in ("stop", "restart"):
        _stop_if_running()
    if action in ("start", "restart"):
        _start()
    print(f"Служба {SERVICE_NAME}: {_STATE_NAMES.get(_state(), '?')}")
