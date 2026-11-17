import asyncio

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
@click.option("--proxy-headers/--no-proxy-headers", default=True, show_default=True,
              help="Trust X-Forwarded-* from a reverse proxy (Caddy, nginx).")
def serve(host: str, port: int, reload: bool, proxy_headers: bool) -> None:
    """Start the web server (web panel and agent API)."""
    uvicorn.run("pc_server.main:app", host=host, port=port, reload=reload,
                proxy_headers=proxy_headers, forwarded_allow_ips="*" if proxy_headers else None)


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
