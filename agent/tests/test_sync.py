import json
import threading
from datetime import date

import pytest

from pc_agent.activity import Activity, ActivityHub
from pc_agent.rules_file import load_rules, save_rules
from pc_agent.storage import Store
from pc_agent.sync import ServerError, SyncWorker, connect, disconnect, load_config
from pc_shared.events import Event, EventKind, local_now
from pc_shared.rules import DayRules, Rules


class FakeServer:
    """Records requests and answers like the real server would."""

    def __init__(self):
        self.requests = []
        self.response = {"rules_version": 0, "commands": []}
        self.fail: ServerError | None = None

    def __call__(self, url, payload, token):
        if self.fail:
            raise self.fail
        self.requests.append((url, payload, token))
        if url.endswith("/register"):
            return {"device_id": 7, "token": "tok-123"}
        return self.response


@pytest.fixture
def env(tmp_path):
    save_rules(tmp_path / "rules.json",
               Rules(child_user="kid", weekdays=DayRules(limit_minutes=60),
                     weekends=DayRules(limit_minutes=60)))
    server = FakeServer()
    connect(tmp_path, "http://pc.local:8000/", "12345678", "Kid PC",
            restrict=False, transport=server)
    hub = ActivityHub()
    worker = SyncWorker(tmp_path, hub, threading.Event(), transport=server,
                        timezone=lambda: "Test Zone")
    store = Store(tmp_path / "agent.db")
    yield tmp_path, server, hub, worker, store
    store.close()


def sync(worker, store, tmp_path):
    return worker.sync_once(store, load_config(tmp_path))


def test_connect_sends_rules_and_saves_token(env):
    tmp_path, server, *_ = env
    url, payload, token = server.requests[0]
    assert url == "http://pc.local:8000/api/v1/agent/register" and token is None
    assert payload["code"] == "12345678" and payload["rules"]["child_user"] == "kid"
    assert load_config(tmp_path) == {"url": "http://pc.local:8000", "device_id": 7,
                                     "token": "tok-123"}


def test_sync_sends_status_events_and_usage(env):
    tmp_path, server, hub, worker, store = env
    store.add_usage(local_now().date(), 600)
    store.add_app_usage(local_now().date(), "chrome.exe", 300)
    store.add_event(Event(kind=EventKind.APP_BLOCKED, data={"name": "roblox"}))
    hub.set_status({"child_logged_in": True, "idle": False})
    hub.set_expected_pid(1)
    hub.push(Activity(idle_seconds=1, app="chrome.exe", title="YouTube", received=10**9))

    interval = sync(worker, store, tmp_path)

    url, payload, token = server.requests[-1]
    assert url.endswith("/api/v1/agent/sync") and token == "tok-123"
    assert payload["status"]["remaining_minutes"] > 0
    assert payload["status"]["child_logged_in"] is True
    assert payload["status"]["timezone"] == "Test Zone"
    assert [e["kind"] for e in payload["events"]][-1] == "app_blocked"
    assert payload["days"][-1]["used_minutes"] == 10
    assert payload["apps"] == [{"day": local_now().date().isoformat(), "app": "chrome.exe",
                                "minutes": 5.0}]
    assert payload["rules_version"] == 1  # the server got our rules at registration
    assert interval == 15

    sync(worker, store, tmp_path)  # events are sent only once
    assert server.requests[-1][1]["events"] == []


def test_failed_sync_keeps_events_for_later(env):
    tmp_path, server, hub, worker, store = env
    store.add_event(Event(kind=EventKind.WARNING))
    server.fail = ServerError("сервер недоступен")
    with pytest.raises(ServerError):
        sync(worker, store, tmp_path)
    server.fail = None
    sync(worker, store, tmp_path)
    assert [e["kind"] for e in server.requests[-1][1]["events"]][-1] == "warning"


def test_new_rules_are_saved(env):
    tmp_path, server, hub, worker, store = env
    new_rules = Rules(child_user="kid", blocked_apps=["roblox*"])
    server.response = {"rules_version": 3, "rules": new_rules.model_dump(mode="json"),
                       "commands": []}
    sync(worker, store, tmp_path)
    assert load_rules(tmp_path / "rules.json").blocked_apps == ["roblox*"]
    server.response = {"rules_version": 3, "commands": []}
    sync(worker, store, tmp_path)
    assert server.requests[-1][1]["rules_version"] == 3


def test_commands_applied_once_and_acked(env):
    tmp_path, server, hub, worker, store = env
    server.response = {"rules_version": 1, "commands": [
        {"id": 5, "action": "bonus", "minutes": 30}, {"id": 6, "action": "block"}]}
    assert sync(worker, store, tmp_path) == 2  # results go back quickly
    sync(worker, store, tmp_path)  # redelivered before the server saw the acks
    state = store.load_day(local_now().date())
    assert state.bonus_minutes == 30 and state.blocked  # applied only once
    acks = server.requests[-1][1]["acks"]
    assert [(a["id"], a["ok"]) for a in acks] == [(5, True), (6, True)]
    commands = [e for e in store.recent_events(20) if e.kind == EventKind.PARENT_COMMAND]
    assert [e.data["source"] for e in commands] == ["server", "server"]


def test_disconnect(env):
    tmp_path, *_ = env
    assert disconnect(tmp_path) and load_config(tmp_path) is None
    assert not disconnect(tmp_path)


def test_yesterday_included_for_midnight(env):
    tmp_path, server, hub, worker, store = env
    sync(worker, store, tmp_path)
    days = [date.fromisoformat(d["day"]) for d in server.requests[-1][1]["days"]]
    assert days[1] - days[0] == local_now().date() - days[0] and len(days) == 2
    assert json.dumps(server.requests[-1][1])  # payload is plain JSON
