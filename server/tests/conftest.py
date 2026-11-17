import os

# Before pc_server is imported: no files on disk during tests.
os.environ["PC_SERVER_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["PC_SERVER_SECRET_KEY"] = "test-secret"

import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from pc_server.db.base import Base  # noqa: E402
from pc_server.db.session import get_db  # noqa: E402
from pc_server.main import app  # noqa: E402
from pc_server.web import routes  # noqa: E402
from pc_shared.passwords import AttemptLimiter  # noqa: E402

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="session")
async def engine():
    eng = create_async_engine(TEST_DATABASE_URL)

    # The sqlite driver handles BEGIN/SAVEPOINT itself and breaks rollback of savepoints;
    # let SQLAlchemy emit them so every test is rolled back completely.
    @event.listens_for(eng.sync_engine, "connect")
    def _no_driver_transactions(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(eng.sync_engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN")

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db_session(engine) -> AsyncSession:
    async with engine.connect() as conn, conn.begin() as trans:
        # Service code commits; savepoints keep each test inside one rolled-back transaction.
        session = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )()
        yield session
        await session.close()
        await trans.rollback()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession, monkeypatch) -> AsyncClient:
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(routes, "login_limiter", AttemptLimiter(max_failures=3, lock_seconds=300))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
