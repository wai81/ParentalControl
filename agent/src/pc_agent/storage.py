"""Local SQLite storage: daily counters and the activity log.

Every write is a single atomic statement, so the running agent and CLI commands
(bonus, block) can safely use the same database at the same time.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from pc_agent.policy import DayState
from pc_shared.events import Event, EventKind

_SCHEMA = """
CREATE TABLE IF NOT EXISTS day_usage (
    day           TEXT PRIMARY KEY,
    used_seconds  REAL    NOT NULL DEFAULT 0,
    bonus_minutes INTEGER NOT NULL DEFAULT 0,
    blocked       INTEGER NOT NULL DEFAULT 0,
    warned        TEXT    NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT    NOT NULL,
    kind   TEXT    NOT NULL,
    data   TEXT    NOT NULL DEFAULT '{}',
    synced INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS app_usage (
    day     TEXT NOT NULL,
    app     TEXT NOT NULL,
    seconds REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, app)
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS events_unsynced ON events (synced) WHERE synced = 0;
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------- daily counters ----------

    def _ensure_day(self, day: date) -> str:
        key = day.isoformat()
        self._conn.execute("INSERT OR IGNORE INTO day_usage (day) VALUES (?)", (key,))
        return key

    def load_day(self, day: date) -> DayState:
        key = self._ensure_day(day)
        row = self._conn.execute(
            "SELECT used_seconds, bonus_minutes, blocked, warned FROM day_usage WHERE day = ?",
            (key,),
        ).fetchone()
        used, bonus, blocked, warned = row
        return DayState(
            day=day,
            used_seconds=float(used),
            bonus_minutes=int(bonus),
            blocked=bool(blocked),
            warned=frozenset(int(w) for w in json.loads(warned)),
        )

    def add_usage(self, day: date, seconds: float) -> None:
        key = self._ensure_day(day)
        self._conn.execute(
            "UPDATE day_usage SET used_seconds = used_seconds + ? WHERE day = ?", (seconds, key)
        )

    def add_bonus(self, day: date, minutes: int) -> None:
        key = self._ensure_day(day)
        self._conn.execute(
            "UPDATE day_usage SET bonus_minutes = bonus_minutes + ? WHERE day = ?", (minutes, key)
        )

    def set_blocked(self, day: date, blocked: bool) -> None:
        key = self._ensure_day(day)
        self._conn.execute("UPDATE day_usage SET blocked = ? WHERE day = ?", (int(blocked), key))

    def set_warned(self, day: date, warned: frozenset[int]) -> None:
        key = self._ensure_day(day)
        self._conn.execute(
            "UPDATE day_usage SET warned = ? WHERE day = ?", (json.dumps(sorted(warned)), key)
        )

    def history(self, days: int = 14) -> list[tuple[date, float]]:
        rows = self._conn.execute(
            "SELECT day, used_seconds FROM day_usage ORDER BY day DESC LIMIT ?", (days,)
        ).fetchall()
        return [(date.fromisoformat(d), used / 60) for d, used in rows]

    # ---------- agent's own settings ----------

    def get_setting(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ---------- time per application ----------

    def add_app_usage(self, day: date, app: str, seconds: float) -> None:
        self._conn.execute(
            "INSERT INTO app_usage (day, app, seconds) VALUES (?, ?, ?) "
            "ON CONFLICT (day, app) DO UPDATE SET seconds = seconds + excluded.seconds",
            (day.isoformat(), app.lower(), seconds),
        )

    def app_usage(self, day: date, limit: int = 20) -> list[tuple[str, float]]:
        rows = self._conn.execute(
            "SELECT app, seconds FROM app_usage WHERE day = ? ORDER BY seconds DESC LIMIT ?",
            (day.isoformat(), limit),
        ).fetchall()
        return [(app, seconds / 60) for app, seconds in rows]

    # ---------- retention ----------

    def purge(self, today: date, events_days: int, usage_days: int) -> int:
        """Delete old log entries and counters. Returns the number of deleted events."""
        events_before = (today - timedelta(days=events_days)).isoformat()
        usage_before = (today - timedelta(days=usage_days)).isoformat()
        deleted = self._conn.execute("DELETE FROM events WHERE ts < ?", (events_before,)).rowcount
        self._conn.execute("DELETE FROM app_usage WHERE day < ?", (usage_before,))
        self._conn.execute("DELETE FROM day_usage WHERE day < ?", (usage_before,))
        return deleted

    # ---------- activity log ----------

    def add_event(self, event: Event) -> None:
        self._conn.execute(
            "INSERT INTO events (ts, kind, data) VALUES (?, ?, ?)",
            (event.ts.isoformat(), event.kind.value, json.dumps(event.data, ensure_ascii=False)),
        )

    def unsynced_events(self, limit: int = 500) -> list[tuple[int, Event]]:
        """Oldest log entries not yet sent to the server, with their local ids."""
        rows = self._conn.execute(
            "SELECT id, ts, kind, data FROM events WHERE synced = 0 ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        return [
            (row_id, Event(ts=datetime.fromisoformat(ts), kind=EventKind(kind),
                           data=json.loads(data)))
            for row_id, ts, kind, data in rows
        ]

    def mark_synced(self, ids: list[int]) -> None:
        self._conn.executemany("UPDATE events SET synced = 1 WHERE id = ?", [(i,) for i in ids])

    def recent_events(self, limit: int = 50) -> list[Event]:
        rows = self._conn.execute(
            "SELECT ts, kind, data FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            Event(ts=datetime.fromisoformat(ts), kind=EventKind(kind), data=json.loads(data))
            for ts, kind, data in reversed(rows)
        ]
