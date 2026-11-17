"""Windows service wrapper (runs the agent as SYSTEM, starts with Windows).

Intended for the packaged pcagent.exe (PyInstaller). From a source checkout use
`pcagent run` instead: pywin32 services from a virtualenv need extra setup.
"""

from __future__ import annotations

import subprocess
import sys
import threading

import servicemanager
import win32service
import win32serviceutil

from pc_agent.runner import Paths, default_data_dir, run_agent

SERVICE_NAME = "ParentalControlAgent"


class AgentService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = "Parental Control Agent"
    _svc_description_ = "Родительский контроль: учёт времени, ограничения и журнал активности."

    def __init__(self, args: list[str]) -> None:
        super().__init__(args)
        self._stop = threading.Event()

    def SvcStop(self) -> None:  # noqa: N802 (pywin32 API name)
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self._stop.set()

    def SvcDoRun(self) -> None:  # noqa: N802 (pywin32 API name)
        servicemanager.LogInfoMsg(f"{SERVICE_NAME} starting")
        run_agent(Paths(default_data_dir()), stop=self._stop)


def run_dispatcher() -> None:
    """Entry point when the Service Control Manager starts pcagent.exe without arguments."""
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(AgentService)
    servicemanager.StartServiceCtrlDispatcher()


def harden_data_dir(paths: Paths) -> None:
    """Only SYSTEM and Administrators may change agent files; users may only read them.

    Without this, a standard user could create rules.json in ProgramData before the
    parent does, because ProgramData lets users create new files by default.
    """
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "icacls", str(paths.data_dir), "/inheritance:r",
            "/grant:r", "*S-1-5-18:(OI)(CI)F",      # SYSTEM
            "/grant:r", "*S-1-5-32-544:(OI)(CI)F",  # Administrators
            "/grant:r", "*S-1-5-32-545:(OI)(CI)RX",  # Users
        ],
        check=True,
        capture_output=True,
    )


def install(paths: Paths) -> None:
    if not getattr(sys, "frozen", False):
        print(
            "Внимание: служба рассчитана на собранный pcagent.exe. "
            "Из исходников используйте 'pcagent run'."
        )
    harden_data_dir(paths)
    win32serviceutil.HandleCommandLine(
        AgentService, argv=[sys.argv[0], "--startup", "auto", "install"]
    )
    # Restart automatically 5 seconds after a crash or kill.
    subprocess.run(
        ["sc", "failure", SERVICE_NAME, "reset=", "86400",
         "actions=", "restart/5000/restart/5000/restart/5000"],
        check=True,
        capture_output=True,
    )


def control(command: str) -> None:
    """command: start | stop | remove | restart."""
    win32serviceutil.HandleCommandLine(AgentService, argv=[sys.argv[0], command])
