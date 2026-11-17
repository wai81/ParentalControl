import sys
from pathlib import Path

import pytest

from pc_agent import service


def make_build(root: Path, version: str) -> Path:
    root.mkdir(parents=True)
    (root / "pcagent.exe").write_text(version)
    (root / "_internal").mkdir()
    (root / "_internal" / "lib.dll").write_text(version)
    return root / "pcagent.exe"


def test_upgrade_replaces_files_atomically(tmp_path, monkeypatch):
    target = tmp_path / "Program Files" / "agent"
    make_build(target, "old")
    (target / "stale.dll").write_text("from an older version")
    monkeypatch.setattr(sys, "executable", str(make_build(tmp_path / "dist", "new")))

    exe = service._copy_build(target)

    assert exe == target / "pcagent.exe"
    assert exe.read_text() == "new"
    assert (target / "_internal" / "lib.dll").read_text() == "new"
    assert not (target / "stale.dll").exists()
    assert sorted(p.name for p in target.parent.iterdir()) == ["agent"]  # no .new/.old left


def test_failed_swap_keeps_old_files(tmp_path, monkeypatch):
    target = tmp_path / "agent"
    make_build(target, "old")
    monkeypatch.setattr(sys, "executable", str(make_build(tmp_path / "dist", "new")))
    real_rename = Path.rename

    def rename(self, dest):
        if self.name == "agent.new":
            raise PermissionError("file in use")
        return real_rename(self, dest)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(PermissionError):
        service._copy_build(target)
    assert (target / "pcagent.exe").read_text() == "old"


def test_refuses_foreign_folder(tmp_path, monkeypatch):
    target = tmp_path / "agent"
    target.mkdir()
    (target / "important.txt").write_text("user data")
    monkeypatch.setattr(sys, "executable", str(make_build(tmp_path / "dist", "new")))
    with pytest.raises(service.ServiceError):
        service._copy_build(target)
    assert (target / "important.txt").exists()
