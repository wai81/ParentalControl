import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from pc_server.api.v1.routes import agent, health
from pc_server.core.config import settings
from pc_server.core.exceptions import PcServerError
from pc_server.db.base import Base
from pc_server.db.session import engine
from pc_server.web import routes as web
from pc_server.web.auth import LoginRequired

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Tables are created on start. Schema changes will need Alembic migrations.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.resolved_secret_key(),
        session_cookie="pc_session",
        max_age=30 * 24 * 3600,
        same_site="strict",
        https_only=settings.secure_cookies,
    )
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"),
              name="static")
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(agent.router, prefix="/api/v1")
    app.include_router(web.router)

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired) -> Response:
        if request.url.path.endswith("/status"):  # background refresh: no redirect page
            return JSONResponse(status_code=401, content={"detail": exc.detail})
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(PcServerError)
    async def server_error(request: Request, exc: PcServerError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422, content={"detail": "Неверные данные", "details": exc.errors()}
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Непредвиденная ошибка."})

    return app


app = create_app()
