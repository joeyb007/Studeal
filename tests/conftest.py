from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.api.main import app
from dealbot.db.models import Base


@pytest.fixture()
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture()
def client(db_factory, monkeypatch):
    """TestClient with all route session dependencies patched to in-memory SQLite."""
    factory = db_factory

    @asynccontextmanager
    async def _test_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    monkeypatch.setattr("dealbot.api.routes.auth.get_async_session", _test_session)
    monkeypatch.setattr("dealbot.api.routes.deals.get_async_session", _test_session)
    monkeypatch.setattr("dealbot.api.routes.watchlists.get_async_session", _test_session)
    monkeypatch.setattr("dealbot.api.routes.alerts.get_async_session", _test_session)
    monkeypatch.setattr("dealbot.api.auth.get_async_session", _test_session)
    return TestClient(app)
