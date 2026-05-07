from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.api.auth import get_current_user
from dealbot.api.main import app
from dealbot.db.models import Base, User


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
    """TestClient with session dependencies patched. Auth routes still use real rate limiting —
    requires Redis. Use authed_client for deal/watchlist endpoint tests."""
    factory = db_factory

    @asynccontextmanager
    async def _test_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    monkeypatch.setattr("dealbot.api.routes.auth.get_async_session", _test_session)
    monkeypatch.setattr("dealbot.api.routes.deals.get_async_session", _test_session)
    monkeypatch.setattr("dealbot.api.routes.watchlists.get_async_session", _test_session)
    monkeypatch.setattr("dealbot.api.auth.get_async_session", _test_session)
    return TestClient(app)


@pytest.fixture()
def authed_client(db_factory, monkeypatch):
    """TestClient with session dependencies patched and get_current_user overridden.
    Use this for endpoints that require auth but don't test the auth flow itself."""
    factory = db_factory

    @asynccontextmanager
    async def _test_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    fake_user = User()
    fake_user.id = 1
    fake_user.email = "test@example.com"
    fake_user.is_pro = False

    async def _fake_current_user():
        return fake_user

    monkeypatch.setattr("dealbot.api.routes.deals.get_async_session", _test_session)
    monkeypatch.setattr("dealbot.api.routes.watchlists.get_async_session", _test_session)
    monkeypatch.setattr("dealbot.api.auth.get_async_session", _test_session)

    app.dependency_overrides[get_current_user] = _fake_current_user
    yield TestClient(app)
    app.dependency_overrides.clear()
