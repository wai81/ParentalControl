"""The parent's password for the agent (separate from Windows accounts).

Only a salted scrypt hash is stored, in parent.key, which only SYSTEM and
Administrators may read: the child cannot copy the hash to guess the password
offline. Guessing through the service is slowed down by `AttemptLimiter`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pc_shared.passwords import AttemptLimiter, hash_password, verify_password

__all__ = ["AttemptLimiter", "hash_password", "verify_password", "load_record", "save_password"]

MIN_LENGTH = 6


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
        restrict_file(path)


def restrict_file(path: Path) -> None:
    """Only SYSTEM and Administrators may read or change the file."""
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
