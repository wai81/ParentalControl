"""Loads rules.json and reloads it when the file changes."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from pc_shared.rules import Rules

log = logging.getLogger(__name__)


def load_rules(path: Path) -> Rules:
    return Rules.model_validate_json(path.read_text(encoding="utf-8-sig"))


def save_rules(path: Path, rules: Rules) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(rules.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)


class RulesFile:
    """Keeps the last valid rules: a broken edit of rules.json never disables control."""

    def __init__(self, path: Path, on_error: Callable[[str], None] | None = None) -> None:
        self._path = path
        self._on_error = on_error
        self._rules = Rules()
        self._mtime: int | None = None

    def get(self) -> Rules:
        try:
            mtime = self._path.stat().st_mtime_ns
        except FileNotFoundError:
            return self._rules
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                self._rules = load_rules(self._path)
                log.info("Правила загружены из %s", self._path)
            except (ValidationError, ValueError, OSError) as exc:
                message = f"Ошибка в {self._path.name}, используются прежние правила: {exc}"
                log.error(message)
                if self._on_error:
                    self._on_error(message)
        return self._rules
