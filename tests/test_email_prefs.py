"""Email preferences: signed one-click unsubscribe + per-type toggles
(2026-08-12 accounts/email/billing spec, stage 1)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dealbot.api.routes.email_prefs import (
    make_unsubscribe_token,
    parse_unsubscribe_token,
)
from dealbot.db.models import User


# ---- token layer -----------------------------------------------------------


def test_token_roundtrips_user_and_type():
    token = make_unsubscribe_token(7, "alerts")
    assert parse_unsubscribe_token(token) == (7, "alerts")


def test_token_rejects_unknown_type_at_mint():
    with pytest.raises(ValueError):
        make_unsubscribe_token(7, "marketing")


def test_garbage_and_wrong_scope_tokens_parse_to_none():
    assert parse_unsubscribe_token("not-a-token") is None
    # A real auth token must not work as an unsubscribe token.
    from dealbot.api.auth import create_access_token

    assert parse_unsubscribe_token(create_access_token(7)) is None


# ---- endpoint layer --------------------------------------------------------


@pytest.fixture()
def prefs_client(authed_client, db_factory, monkeypatch):
    factory = db_factory

    @asynccontextmanager
    async def _test_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    monkeypatch.setattr(
        "dealbot.api.routes.email_prefs.get_async_session", _test_session
    )
    return authed_client, factory


async def _seed_user(factory) -> None:
    async with factory() as s:
        s.add(User(id=1, email="test@example.com", hashed_password="x"))
        await s.commit()


@pytest.mark.asyncio
async def test_unsubscribe_flips_exactly_one_pref(prefs_client):
    client, factory = prefs_client
    await _seed_user(factory)

    token = make_unsubscribe_token(1, "alerts")
    resp = client.post("/email/unsubscribe", json={"token": token})
    assert resp.status_code == 200
    assert resp.json()["type"] == "alerts"

    async with factory() as s:
        user = await s.get(User, 1)
        assert user.email_alerts is False
        assert user.email_price_drops is True, "other types untouched"
        assert user.email_digest is True


@pytest.mark.asyncio
async def test_unsubscribe_rejects_bad_token(prefs_client):
    client, factory = prefs_client
    await _seed_user(factory)
    resp = client.post("/email/unsubscribe", json={"token": "junk"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_prefs_patch_and_get(prefs_client):
    client, factory = prefs_client
    await _seed_user(factory)

    resp = client.patch("/email/prefs", json={"digest": False})
    assert resp.status_code == 200
    assert resp.json() == {"alerts": True, "price_drops": True, "digest": False}

    resp = client.get("/email/prefs")
    assert resp.json()["digest"] is False
