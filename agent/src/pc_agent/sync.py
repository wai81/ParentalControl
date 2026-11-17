"""Connection to the parent's server.

`pcagent connect` exchanges a one-time pairing code for a device token and
saves it in server.json (readable only by SYSTEM and Administrators). The
service then runs `SyncWorker`: every few seconds it sends the status, new log
entries and usage counters, and applies newer rules and the parent's commands.
Everything keeps working while the server is unreachable; data is sent later.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from pc_agent import __version__
from pc_agent.activity import ActivityHub
from pc_agent.control import apply_command, compute_status
from pc_agent.rules_file import save_rules
from pc_agent.storage import Store
from pc_shared.events import local_now
from pc_shared.protocol import (
    API_PREFIX,
    AppUsageIn,
    CommandAck,
    DayUsageIn,
    DeviceStatus,
    EventIn,
    RegisterRequest,
    RegisterResponse,
    SyncRequest,
    SyncResponse,
)
from pc_shared.rules import Rules

log = logging.getLogger(__name__)

TIMEOUT = 15.0
RETRY_SECONDS = 60.0
REVOKED_RETRY_SECONDS = 600.0
EVENTS_PER_SYNC = 500
_APPLIED_KEY = "applied_commands"
_RULES_VERSION_KEY = "rules_version"

Transport = Callable[[str, dict[str, Any], str | None], dict[str, Any]]


class ServerError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def http_post(url: str, payload: dict[str, Any], token: str | None) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": f"pcagent/{__version__}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", exc.reason)
        except (ValueError, OSError):
            detail = exc.reason
        raise ServerError(str(detail), exc.code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ServerError(f"сервер недоступен: {getattr(exc, 'reason', exc)}") from exc


# ---------- server.json ----------


def config_path(data_dir: Path) -> Path:
    return data_dir / "server.json"


def load_config(data_dir: Path) -> dict[str, Any] | None:
    try:
        return json.loads(config_path(data_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def connect(
    data_dir: Path, server_url: str, code: str, name: str, *,
    restrict: bool = True, transport: Transport = http_post,
) -> RegisterResponse:
    """Register this computer on the server with a pairing code from the web panel."""
    from pc_agent.secret import restrict_file

    server_url = server_url.rstrip("/")
    rules_path = data_dir / "rules.json"
    rules = Rules.model_validate_json(rules_path.read_text("utf-8-sig")) \
        if rules_path.exists() else None
    request = RegisterRequest(code=code, name=name, rules=rules)
    reply = RegisterResponse.model_validate(
        transport(f"{server_url}{API_PREFIX}/register", request.model_dump(mode="json"), None)
    )
    path = config_path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"url": server_url, "device_id": reply.device_id,
                               "token": reply.token}), encoding="utf-8")
    tmp.replace(path)
    if restrict:
        restrict_file(path)
    with Store(data_dir / "agent.db") as store:
        # The server got our rules as version 1: do not download them back.
        store.set_setting(_RULES_VERSION_KEY, "1" if rules else "0")
    return reply


def disconnect(data_dir: Path) -> bool:
    path = config_path(data_dir)
    if not path.exists():
        return False
    path.unlink()
    return True


# ---------- sync ----------


class SyncWorker(threading.Thread):
    def __init__(
        self, data_dir: Path, hub: ActivityHub | None, stop: threading.Event,
        transport: Transport = http_post, timezone: Callable[[], str] = lambda: "",
    ) -> None:
        super().__init__(name="server-sync", daemon=True)
        self._data_dir = data_dir
        self._hub = hub
        self._stop_event = stop
        self._transport = transport
        self._timezone = timezone
        self._acks: list[CommandAck] = []
        self._last_error = ""

    def run(self) -> None:
        while not self._stop_event.is_set():
            config = load_config(self._data_dir)
            # Not connected yet: look for server.json again in 30 s.
            delay = 30.0 if config is None else self._sync_safely(config)
            self._stop_event.wait(delay)

    def _sync_safely(self, config: dict[str, Any]) -> float:
        try:
            with Store(self._data_dir / "agent.db") as store:
                interval = self.sync_once(store, config)
            if self._last_error:
                log.info("Связь с сервером восстановлена")
                self._last_error = ""
            return float(interval)
        except ServerError as exc:
            if str(exc) != self._last_error:
                log.warning("Синхронизация с сервером не удалась: %s", exc)
                self._last_error = str(exc)
            if exc.status == 401:
                return REVOKED_RETRY_SECONDS  # the device was deleted in the web panel
            return RETRY_SECONDS
        except Exception:
            log.exception("Ошибка синхронизации с сервером")
            return RETRY_SECONDS

    def build_request(self, store: Store) -> tuple[SyncRequest, list[int]]:
        now = local_now()
        today, yesterday = now.date(), now.date() - timedelta(days=1)
        status = compute_status(self._data_dir, now)
        hub_status = self._hub.status() if self._hub else {}
        activity = self._hub.latest() if self._hub else None
        device_status = DeviceStatus(
            **status,
            child_logged_in=bool(hub_status.get("child_logged_in")),
            idle=bool(hub_status.get("idle")),
            app=activity.app if activity else None,
            title=activity.title if activity else None,
            agent_version=__version__,
            timezone=self._timezone(),
        )
        events = store.unsynced_events(EVENTS_PER_SYNC)
        days = []
        for day in (yesterday, today):
            state = store.load_day(day)
            days.append(DayUsageIn(day=day, used_minutes=round(state.used_minutes, 2),
                                   bonus_minutes=state.bonus_minutes, blocked=state.blocked))
        apps = [
            AppUsageIn(day=day, app=app, minutes=round(minutes, 2))
            for day in (yesterday, today) for app, minutes in store.app_usage(day, limit=50)
        ]
        request = SyncRequest(
            status=device_status,
            events=[EventIn(local_id=i, ts=e.ts, kind=e.kind.value, data=e.data)
                    for i, e in events],
            days=days,
            apps=apps,
            acks=list(self._acks),
            rules_version=int(store.get_setting(_RULES_VERSION_KEY) or 0),
        )
        return request, [i for i, _ in events]

    def sync_once(self, store: Store, config: dict[str, Any]) -> int:
        request, event_ids = self.build_request(store)
        raw = self._transport(
            f"{config['url']}{API_PREFIX}/sync", request.model_dump(mode="json"), config["token"]
        )
        response = SyncResponse.model_validate(raw)
        store.mark_synced(event_ids)
        self._acks = []  # delivered with this request
        if response.rules is not None:
            save_rules(self._data_dir / "rules.json", response.rules)
            store.set_setting(_RULES_VERSION_KEY, str(response.rules_version))
            log.info("Получены новые правила с сервера (версия %s)", response.rules_version)
        self._apply_commands(store, response)
        if self._acks:
            return 2  # report command results soon, the parent is waiting for them
        return max(5, min(response.sync_interval, 300))

    def _apply_commands(self, store: Store, response: SyncResponse) -> None:
        applied: list[int] = json.loads(store.get_setting(_APPLIED_KEY) or "[]")
        for command in response.commands:
            if command.id in applied:
                # Done before, but the server did not get our confirmation yet.
                self._acks.append(CommandAck(id=command.id, ok=True, message="выполнено"))
                continue
            result = apply_command(store, command.action, command.minutes, local_now(),
                                   source="server")
            applied.append(command.id)
            self._acks.append(CommandAck(id=command.id, ok=bool(result["ok"]),
                                         message=str(result.get("message", ""))))
        store.set_setting(_APPLIED_KEY, json.dumps(applied[-200:]))
