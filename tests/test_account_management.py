"""Password reset, change-password, and account deletion
(2026-08-12 accounts/email/billing spec, stage 2)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dealbot.api.auth import hash_password, verify_password
from dealbot.api.routes.auth import make_reset_token, parse_reset_token
from dealbot.db.models import User, Watchlist


@pytest.fixture()
def account_client(authed_client, db_factory, monkeypatch):
    factory = db_factory

    @asynccontextmanager
    async def _test_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    monkeypatch.setattr("dealbot.api.routes.auth.get_async_session", _test_session)

    sent: list[tuple[str, str]] = []

    async def fake_send(to, subject, body, html=None):
        sent.append((to, subject))
        return True

    monkeypatch.setattr("dealbot.notifications.email.send_email", fake_send)
    return authed_client, factory, sent


async def _seed(factory, *, password: str | None = "hunter22") -> None:
    async with factory() as s:
        s.add(User(
            id=1, email="test@example.com",
            hashed_password=hash_password(password) if password else "",
        ))
        await s.commit()


# ---- reset tokens ----------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_token_dies_with_password_change(account_client):
    _client, factory, _sent = account_client
    await _seed(factory)
    async with factory() as s:
        user = await s.get(User, 1)
        token = make_reset_token(user)
        assert parse_reset_token(token) is not None
        user.hashed_password = hash_password("something-new")
        await s.commit()

    resp = _client.post("/auth/reset-confirm",
                        json={"token": token, "new_password": "irrelevant1"})
    assert resp.status_code == 400, "old link must die when the password changes"


@pytest.mark.asyncio
async def test_reset_confirm_sets_new_password_and_single_use(account_client):
    client, factory, _sent = account_client
    await _seed(factory)
    async with factory() as s:
        token = make_reset_token(await s.get(User, 1))

    resp = client.post("/auth/reset-confirm",
                       json={"token": token, "new_password": "new-password-9"})
    assert resp.status_code == 200
    async with factory() as s:
        user = await s.get(User, 1)
        assert verify_password("new-password-9", user.hashed_password)

    # Same token again: fingerprint no longer matches → dead.
    resp = client.post("/auth/reset-confirm",
                       json={"token": token, "new_password": "another-pass-1"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_reset_request_never_discloses_existence(account_client):
    client, factory, sent = account_client
    await _seed(factory)
    ok = client.post("/auth/reset-request", json={"email": "test@example.com"})
    missing = client.post("/auth/reset-request", json={"email": "ghost@example.com"})
    assert ok.status_code == missing.status_code == 200
    assert ok.json() == missing.json(), "responses must be indistinguishable"
    assert len(sent) == 1, "only the real account got an email"


@pytest.mark.asyncio
async def test_google_only_account_gets_notice_not_reset(account_client):
    client, factory, sent = account_client
    await _seed(factory, password=None)
    resp = client.post("/auth/reset-request", json={"email": "test@example.com"})
    assert resp.status_code == 200
    assert len(sent) == 1
    assert "Google" in sent[0][1]


# ---- change password -------------------------------------------------------


@pytest.mark.asyncio
async def test_change_password_requires_current(account_client):
    client, factory, _sent = account_client
    await _seed(factory)
    bad = client.post("/auth/change-password",
                      json={"current_password": "wrong", "new_password": "long-enough-1"})
    assert bad.status_code == 401

    good = client.post("/auth/change-password",
                       json={"current_password": "hunter22", "new_password": "long-enough-1"})
    assert good.status_code == 200
    async with factory() as s:
        user = await s.get(User, 1)
        assert verify_password("long-enough-1", user.hashed_password)


# ---- deletion --------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_account_cascades_and_checks_password(account_client):
    client, factory, _sent = account_client
    await _seed(factory)
    async with factory() as s:
        # sqlite needs FK enforcement opted in for ON DELETE CASCADE to run;
        # postgres enforces it natively via the migration-declared FKs.
        from sqlalchemy import text

        await s.execute(text("PRAGMA foreign_keys=ON"))
        s.add(Watchlist(user_id=1, name="Aeron watch"))
        await s.commit()

    bad = client.post("/auth/delete-account", json={"password": "wrong"})
    assert bad.status_code == 401

    good = client.post("/auth/delete-account", json={"password": "hunter22"})
    assert good.status_code == 200
    async with factory() as s:
        assert await s.get(User, 1) is None
        watchlists = (await s.execute(select(Watchlist))).scalars().all()
        assert watchlists == [], "FK cascade removes the user's watchlists"


@pytest.mark.asyncio
async def test_delete_aborts_when_stripe_cancel_fails(account_client, monkeypatch):
    client, factory, _sent = account_client
    await _seed(factory)
    async with factory() as s:
        user = await s.get(User, 1)
        user.stripe_subscription_id = "sub_123"
        await s.commit()

    import stripe

    def _boom(sub_id):
        raise stripe.StripeError("stripe down")

    monkeypatch.setattr(stripe.Subscription, "cancel", _boom)
    resp = client.post("/auth/delete-account", json={"password": "hunter22"})
    assert resp.status_code == 502
    async with factory() as s:
        assert await s.get(User, 1) is not None, "never orphan an active subscription"
