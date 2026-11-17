import sqlite3

from click.testing import CliRunner

from pc_server import cli as cli_module


def test_backup_keeps_last_copies(tmp_path, monkeypatch):
    db = tmp_path / "server.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (42)")
    monkeypatch.setattr(cli_module.settings, "data_dir", tmp_path)
    monkeypatch.setattr(cli_module.settings, "database_url", "")
    backups = tmp_path / "backups"
    backups.mkdir()
    for i in range(3):
        (backups / f"server-2026010{i}-000000.db").write_bytes(b"old")

    result = CliRunner().invoke(cli_module.cli, ["backup", "--keep", "2"])

    assert result.exit_code == 0, result.output
    copies = sorted(backups.glob("server-*.db"))
    assert len(copies) == 2 and copies[0].name == "server-20260102-000000.db"
    with sqlite3.connect(copies[-1]) as conn:
        assert conn.execute("SELECT x FROM t").fetchone() == (42,)


def test_serve_trusts_only_local_proxy(monkeypatch):
    calls = {}
    monkeypatch.setattr(cli_module.uvicorn, "run", lambda *a, **kw: calls.update(kw))
    result = CliRunner().invoke(cli_module.cli, ["serve", "--port", "9000"])
    assert result.exit_code == 0, result.output
    assert calls["forwarded_allow_ips"] == "127.0.0.1" and calls["port"] == 9000
