from __future__ import annotations

import logging
import os

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select

from dealbot.api.auth import get_current_user
from dealbot.db.database import get_async_session
from dealbot.db.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")
_SUCCESS_URL = os.environ.get("STRIPE_SUCCESS_URL", "http://localhost:3000/dashboard?upgraded=1")
_CANCEL_URL = os.environ.get("STRIPE_CANCEL_URL", "http://localhost:3000/watchlists")


@router.post("/checkout")
async def create_checkout(current_user: User = Depends(get_current_user)) -> dict:
    """Create a Stripe Checkout Session and return the redirect URL."""
    if current_user.is_pro:
        raise HTTPException(status_code=400, detail="Already a pro member.")

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": _PRICE_ID, "quantity": 1}],
            customer_email=current_user.email,
            client_reference_id=str(current_user.id),
            success_url=_SUCCESS_URL,
            cancel_url=_CANCEL_URL,
        )
        return {"url": session.url}
    except stripe.StripeError as exc:
        logger.error("billing: Stripe checkout error — %s", exc)
        raise HTTPException(status_code=502, detail="Failed to create checkout session.")


@router.post("/portal")
async def create_portal(current_user: User = Depends(get_current_user)) -> dict:
    """Return a Stripe Customer Portal URL for subscription management."""
    if not current_user.is_pro or not current_user.stripe_customer_id:
        raise HTTPException(status_code=403, detail="No active subscription.")

    try:
        session = stripe.billing_portal.Session.create(
            customer=current_user.stripe_customer_id,
            return_url=_CANCEL_URL,
        )
        return {"url": session.url}
    except stripe.StripeError as exc:
        logger.error("billing: Stripe portal error — %s", exc)
        raise HTTPException(status_code=502, detail="Failed to create portal session.")


@router.post("/webhook")
async def stripe_webhook(request: Request) -> dict:
    """
    Handle Stripe webhook events.
    Verifies the signature, then sets is_pro based on subscription state.
    """
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, _WEBHOOK_SECRET)
    except stripe.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type in ("customer.subscription.created", "customer.subscription.updated"):
        customer_id = data["customer"]
        subscription_id = data["id"]
        is_active = data["status"] in ("active", "trialing")
        await _set_pro(customer_id, subscription_id, is_active)

    elif event_type == "customer.subscription.deleted":
        customer_id = data["customer"]
        await _set_pro(customer_id, None, False)

    elif event_type == "checkout.session.completed":
        # Capture customer_id on first successful checkout
        user_id = data.get("client_reference_id")
        customer_id = data.get("customer")
        if user_id and customer_id:
            await _set_stripe_customer(int(user_id), customer_id)

    return {"received": True}


async def _set_stripe_customer(user_id: int, customer_id: str) -> None:
    async with get_async_session() as session:
        user = await session.get(User, user_id)
        if user:
            user.stripe_customer_id = customer_id
            await session.commit()


async def _set_pro(customer_id: str, subscription_id: str | None, is_pro: bool) -> None:
    async with get_async_session() as session:
        result = await session.execute(
            select(User).where(User.stripe_customer_id == customer_id)
        )
        user = result.scalar_one_or_none()
        if user:
            user.is_pro = is_pro
            user.stripe_subscription_id = subscription_id
            await session.commit()
            logger.info("billing: user %d is_pro=%s", user.id, is_pro)
        else:
            logger.warning("billing: no user found for customer_id=%s", customer_id)
