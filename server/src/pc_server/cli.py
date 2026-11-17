import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path

import click
import uvicorn

from pc_server.core.config import settings


@click.group()
def cli() -> None:
    """Parental control server."""


@cli.command()
@click.option("--host", default="0.0.0.0", show_default=True, help="Bind host.")
@click.option("--port", default=8000, show_default=True, type=int, help="Bind port.")
@click.option("--reload", is_flag=True, default=settings.debug, help="Reload on code changes.")
@click.option("--forwarded-allow-ips", default="127.0.0.1", show_default=True,
              help="Proxies whose X-Forwarded-* headers are trusted (Tailscale Serve, Caddy "
                   "on the same machine). Other clients cannot fake their address or https.")
def serve(host: str, port: int, reload: bool, forwarded_allow_ips: str) -> None:
    """Start the web server (web panel and agent API)."""
    uvicorn.run("pc_server.main:app", host=host, port=port, reload=reload,
                proxy_headers=True, forwarded_allow_ips=forwarded_allow_ips)


@cli.command("set-password")
@click.password_option("--password", prompt="Новый пароль родителя для веб-панели",
                       confirmation_prompt="Повторите пароль")
def set_password(password: str) -> None:
    """Set the parent's password for the web panel."""
    if len(password) < 8:
        raise click.ClickException("Пароль должен быть не короче 8 символов.")

    async def run() -> None:
        from pc_server.db.base import Base
        from pc_server.db.session import AsyncSessionLocal, engine
        from pc_server.services.devices import set_parent_password

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSessionLocal() as db:
            await set_parent_password(db, password)
        await engine.dispose()

    asyncio.run(run())
    click.echo("Пароль сохранён.")


@cli.command()
@click.option("--keep", default=14, show_default=True, type=int, help="How many copies to keep.")
def backup(keep: int) -> None:
    """Copy the database to data_dir/backups (safe while the server is running)."""
    if settings.database_url:
        raise click.ClickException("Резервная копия делается только для базы SQLite по умолчанию.")
    source = settings.data_dir / "server.db"
    if not source.exists():
        raise click.ClickException(f"База не найдена: {source}")
    folder = settings.data_dir / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"server-{datetime.now():%Y%m%d-%H%M%S}.db"
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)  # consistent copy even during writes
    old = sorted(folder.glob("server-*.db"))[:-keep] if keep > 0 else []
    for path in old:
        Path(path).unlink()
    click.echo(f"Копия сохранена: {target}" + (f", удалено старых: {len(old)}" if old else ""))
