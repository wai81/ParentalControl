"""Packs the server for a Raspberry Pi: dist/pc-server-<version>.tar.gz

    uv run python scripts/package-server.py

Contains only what `uv sync --package pc-server` needs, with Unix line endings
for scripts and systemd units, and the executable bit on install.sh.
On the Pi:  tar xzf pc-server-*.tar.gz && cd pc-server-* && sudo ./server/deploy/install.sh
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDE = [
    "pyproject.toml",
    "uv.lock",
    "shared/pyproject.toml",
    "shared/src",
    "server/pyproject.toml",
    "server/README.md",
    "server/src",
    "server/deploy",
    # The workspace member must be present for uv to read the lock; its code is not installed.
    "agent/pyproject.toml",
]
TEXT_SUFFIXES = {".py", ".toml", ".md", ".html", ".css", ".sh", ".service", ".timer", ".env",
                 ".lock", ".txt"}
SKIP_PARTS = {"__pycache__", ".pytest_cache", "tests"}


def version() -> str:
    text = (ROOT / "server" / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'^version = "([^"]+)"', text, re.MULTILINE).group(1)


def files() -> list[Path]:
    result = []
    for item in INCLUDE:
        path = ROOT / item
        if path.is_file():
            result.append(path)
        else:
            result += [p for p in sorted(path.rglob("*"))
                       if p.is_file() and not SKIP_PARTS & set(p.parts)]
    return result


def build_css() -> None:
    """Rebuild the Tailwind CSS so the package never ships a stale app.css."""
    npm = shutil.which("npm")
    if npm is None:
        print("npm not found: packing the committed app.css as is")
        return
    server = ROOT / "server"
    if not (server / "node_modules").exists():
        subprocess.run([npm, "ci"], cwd=server, check=True)
    subprocess.run([npm, "run", "build:css"], cwd=server, check=True)


def main() -> Path:
    build_css()
    name = f"pc-server-{version()}"
    target = ROOT / "dist" / f"{name}.tar.gz"
    target.parent.mkdir(exist_ok=True)
    now = int(time.time())
    with tarfile.open(target, "w:gz") as tar:
        for path in files():
            data = path.read_bytes()
            if path.suffix in TEXT_SUFFIXES:
                data = data.replace(b"\r\n", b"\n")  # bash and systemd need LF
            info = tarfile.TarInfo(f"{name}/{path.relative_to(ROOT).as_posix()}")
            info.size, info.mtime = len(data), now
            info.mode = 0o755 if path.suffix == ".sh" else 0o644
            tar.addfile(info, io.BytesIO(data))
    print(f"Packed: {target} ({target.stat().st_size // 1024} KB)")
    return target


if __name__ == "__main__":
    main()
