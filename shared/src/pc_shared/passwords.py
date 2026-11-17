"""Password hashing (scrypt) and a limiter against guessing. Used by agent and server."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import time

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
