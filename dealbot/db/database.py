from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from dealbot.db.models import Base

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/dealbot",
)

# The engine is cached PER EVENT LOOP. Celery runs every task in a fresh
# asyncio.run() loop; a module-level engine binds its connection pool to the
# first task's loop and every later task crashes with "attached to a
# different loop" (caught live in the first real end-to-end hunt). Long-lived
# processes (uvicorn) see exactly one loop, so this is a no-op for the API.
#
# Single-slot cache keyed by loop IDENTITY, holding a strong reference to the
# loop: id()-keyed dicts are unsound because CPython recycles addresses of
# dead loops (observed in tests). Holding the previous loop object alive as
# the cache key makes the `is` comparison unambiguous.
_cache: dict[str, object] = {"loop": None, "engine": None, "sessionmaker": None}


def _get_engine() -> AsyncEngine:
    loop = asyncio.get_running_loop()
    if _cache["loop"] is not loop:
        old = _cache["engine"]
        if old is not None:
            # Prior loop is finished; its sockets died with it —
            # dispose(close=False) just releases references.
            try:
                loop.create_task(old.dispose(close=False))  # type: ignore[union-attr]
            except Exception:
                pass
        _cache["loop"] = loop
        _cache["engine"] = create_async_engine(DATABASE_URL, echo=False)
        _cache["sessionmaker"] = async_sessionmaker(
            bind=_cache["engine"],  # type: ignore[arg-type]
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _cache["engine"]  # type: ignore[return-value]


def _get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    _get_engine()
    return _cache["sessionmaker"]  # type: ignore[return-value]


@asynccontextmanager
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with _get_sessionmaker()() as session:
        yield session


async def create_all_tables() -> None:
    """Create all tables. Used in tests and local dev; prod uses Alembic."""
    async with _get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
