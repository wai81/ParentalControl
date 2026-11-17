"""Paths, logging and the main loop shared by the console mode and the Windows service."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pc_agent.rules_file import RulesFile
from pc_agent.storage import Store
from pc_shared.events import Event, EventKind, local_now

log = logging.getLogger("pc_agent")

DEFAULT_INTERVAL = 5.0


def default_data_dir() -> Path:
    env = os.environ.get("PC_AGENT_DATA")
    if env:
        return Path(env)
    program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    # A separate folder from the PowerShell version (C:\ProgramData\ParentalControl).
    return Path(program_data) / "ParentalControl" / "agent"


@dataclass(frozen=True)
class Paths:
    data_dir: Path

    @property
    def rules(self) -> Path:
        return self.data_dir / "rules.json"

    @property
    def db(self) -> Path:
        return self.data_dir / "agent.db"

    @property
    def log(self) -> Path:
        return self.data_dir / "agent.log"


def setup_logging(paths: Paths, console: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        paths.log, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)


def run_agent(
    paths: Paths,
    *,
    dry_run: bool = False,
    interval: float = DEFAULT_INTERVAL,
    stop: threading.Event | None = None,
    console: bool = False,
) -> None:
    from pc_agent.activity import PIPE_NAME, ActivityHub
    from pc_agent.control import CONTROL_PIPE_NAME, ControlHandler
    from pc_agent.engine import Agent
    from pc_agent.hardening import current_timezone
    from pc_agent.pipe_server import PipeServer, report_handler
    from pc_agent.platform_win import DryRunPlatform, WindowsPlatform
    from pc_agent.sync import SyncWorker

    paths.data_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(paths, console)
    stop = stop or threading.Event()

    with Store(paths.db) as store:

        def rules_error(message: str) -> None:
            store.add_event(Event(kind=EventKind.RULES_ERROR, data={"message": message}))

        rules = RulesFile(paths.rules, on_error=rules_error)
        platform = DryRunPlatform() if dry_run else WindowsPlatform()
        hub = ActivityHub()
        servers = [
            PipeServer(PIPE_NAME, report_handler(hub), accept=hub.accepts, label="Helper",
                       max_clients=2),
            PipeServer(CONTROL_PIPE_NAME, ControlHandler(paths.data_dir).handle,
                       label="Команда родителя"),
        ]
        for server in servers:
            server.start()
        SyncWorker(paths.data_dir, hub, stop, timezone=current_timezone).start()
        agent = Agent(platform, store, rules, hub)

        log.info("Агент запущен (данные: %s, dry-run: %s)", paths.data_dir, dry_run)
        store.add_event(Event(kind=EventKind.AGENT_STARTED, data={"dry_run": dry_run}))
        last_status = ""
        try:
            while True:
                try:
                    result = agent.tick(local_now(), time.monotonic())
                    if console and result.decision:
                        d = result.decision
                        status = (
                            f"сеанс {result.session_id}, активен: {result.active}, "
                            f"использовано {d.used_minutes:.0f} из {d.limit_minutes:.0f} мин, "
                            f"осталось {d.remaining_minutes:.0f}"
                            + (", бездействие" if result.idle else "")
                            + (f", окно: {result.app}" if result.app else "")
                        )
                        if status != last_status:
                            log.info(status)
                            last_status = status
                except Exception as exc:
                    log.exception("Ошибка в цикле агента")
                    store.add_event(Event(kind=EventKind.ERROR, data={"message": repr(exc)}))
                if stop.wait(interval):
                    break
        finally:
            for server in servers:
                server.stop()
            store.add_event(Event(kind=EventKind.AGENT_STOPPED))
            log.info("Агент остановлен")
