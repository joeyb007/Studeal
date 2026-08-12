"""Stripe billing lifecycle: webhook drives is_pro, portal gates on a
subscription (2026-08-12 spec, stage 3)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dealbot.db.models import User


@pytest.fixture()
def billing_client(authed_client, db_factory, monkeypatch):
    factory = db_factory

    @asynccontextmanager
    async def _test_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    monkeypatch.setattr("dealbot.api.routes.billing.get_async_session", _test_session)

    # Signature verification is Stripe's SDK; the contract under test is our
    # event handling. construct_event returns the parsed payload directly.
    import stripe

    def _fake_construct(payload, sig, secret):
        import json

        return json.loads(payload)

    monkeypatch.setattr(stripe.Webhook, "construct_event", _fake_construct)
    return authed_client, factory


async def _seed(factory, **kw) -> None:
    async with factory() as s:
        s.add(User(id=1, email="test@example.com", hashed_password="x", **kw))
        await s.commit()


def _sub_event(event_type: str, *, status: str = "active") -> dict:
    return {
        "type": event_type,
        "data": {"object": {"customer": "cus_1", "id": "sub_1", "status": status}},
    }


@pytest.mark.asyncio
async def test_subscription_created_flips_pro_on(billing_client):
    client, factory = billing_client
    await _seed(factory, stripe_customer_id="cus_1")

    resp = client.post("/billing/webhook", json=_sub_event("customer.subscription.created"))
    assert resp.status_code == 200
    async with factory() as s:
        user = await s.get(User, 1)
        assert user.is_pro is True
        assert user.stripe_subscription_id == "sub_1"


@pytest.mark.asyncio
async def test_subscription_deleted_flips_pro_off(billing_client):
    client, factory = billing_client
    await _seed(factory, stripe_customer_id="cus_1", is_pro=True,
                stripe_subscription_id="sub_1")

    resp = client.post("/billing/webhook", json=_sub_event("customer.subscription.deleted"))
    assert resp.status_code == 200
    async with factory() as s:
        user = await s.get(User, 1)
        assert user.is_pro is False


@pytest.mark.asyncio
async def test_past_due_subscription_is_not_pro(billing_client):
    client, factory = billing_client
    await _seed(factory, stripe_customer_id="cus_1", is_pro=True)

    resp = client.post(
        "/billing/webhook",
        json=_sub_event("customer.subscription.updated", status="past_due"),
    )
    assert resp.status_code == 200
    async with factory() as s:
        assert (await s.get(User, 1)).is_pro is False


@pytest.mark.asyncio
async def test_checkout_completed_captures_customer(billing_client):
    client, factory = billing_client
    await _seed(factory)

    resp = client.post("/billing/webhook", json={
        "type": "checkout.session.completed",
        "data": {"object": {"client_reference_id": "1", "customer": "cus_9"}},
    })
    assert resp.status_code == 200
    async with factory() as s:
        assert (await s.get(User, 1)).stripe_customer_id == "cus_9"


@pytest.mark.asyncio
async def test_portal_requires_active_subscription(billing_client):
    client, factory = billing_client
    await _seed(factory)    # free user, no customer id
    resp = client.post("/billing/portal")
    assert resp.status_code == 403
