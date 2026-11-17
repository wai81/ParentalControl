"""The parent's password for the agent (separate from Windows accounts).

Only a salted scrypt hash is stored, in parent.key, which only SYSTEM and
Administrators may read: the child cannot copy the hash to guess the password
offline. Guessing through the service is slowed down by `AttemptLimiter`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import threading
import time
from pathlib import Path

MIN_LENGTH = 6
_N, _R, _P, _DKLEN = 2**14, 8, 1, 32


def hash_password(password: str) -> dict[str, object]:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return {
        "algo": "scrypt", "n": _N, "r": _R, "p": _P,
        "salt": base64.b64encode(salt).decode(),
        "hash": base64.b64encode(digest).decode(),
    }


def verify_password(password: str, record: dict[str, object]) -> bool:
    if record.get("algo") != "scrypt":
        return False
    salt = base64.b64decode(str(record["salt"]))
    expected = base64.b64decode(str(record["hash"]))
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=int(record["n"]), r=int(record["r"]), p=int(record["p"]), dklen=len(expected),
    )
    return hmac.compare_digest(digest, expected)


def key_path(data_dir: Path) -> Path:
    return data_dir / "parent.key"


def load_record(data_dir: Path) -> dict[str, object] | None:
    try:
        return json.loads(key_path(data_dir).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def save_password(data_dir: Path, password: str, *, restrict: bool = True) -> None:
    """Write parent.key. With `restrict`, only SYSTEM and Administrators can read it."""
    if len(password) < MIN_LENGTH:
        raise ValueError(f"пароль должен быть не короче {MIN_LENGTH} символов")
    path = key_path(data_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(hash_password(password)), encoding="utf-8")
    tmp.replace(path)
    if restrict:
        result = subprocess.run(
            [
                "icacls", str(path), "/inheritance:r",
                "/grant:r", "*S-1-5-18:F",      # SYSTEM
                "/grant:r", "*S-1-5-32-544:F",  # Administrators
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise OSError(f"icacls: {result.stdout.strip() or result.stderr.strip()}")


class AttemptLimiter:
    """After `max_failures` wrong passwords in a row, refuse everything for `lock_seconds`."""

    def __init__(self, max_failures: int = 5, lock_seconds: float = 300.0) -> None:
        self._max = max_failures
        self._lock_seconds = lock_seconds
        self._failures = 0
        self._locked_until = 0.0
        self._mutex = threading.Lock()

    def locked_for(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        with self._mutex:
            return max(0.0, self._locked_until - now)

    def record(self, success: bool, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._mutex:
            if success:
                self._failures = 0
                return
            self._failures += 1
            if self._failures >= self._max:
                self._failures = 0
                self._locked_until = now + self._lock_seconds
