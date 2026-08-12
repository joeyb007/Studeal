"""Email preferences: per-type toggles + one-click unsubscribe.

Unsubscribe links must work without a login (CASL/CAN-SPAM one-click rule),
so they carry a long-lived HMAC-signed token scoped to exactly one
(user, email type) pair — possession of a link can only ever turn that one
email type off.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from jose import JWTError, jwt
from pydantic import BaseModel

from dealbot.api.auth import ALGORITHM, SECRET_KEY, get_current_user
from dealbot.db.database import get_async_session
from dealbot.db.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/email", tags=["email"])

EMAIL_TYPES = ("alerts", "price_drops", "digest")
_PREF_FIELD = {
    "alerts": "email_alerts",
    "price_drops": "email_price_drops",
    "digest": "email_digest",
}
_TOKEN_TTL_DAYS = 365


def make_unsubscribe_token(user_id: int, email_type: str) -> str:
    """Signed, single-purpose token for one user + one email type."""
    if email_type not in EMAIL_TYPES:
        raise ValueError(f"unknown email type: {email_type!r}")
    payload = {
        "sub": str(user_id),
        "scope": "unsubscribe",
        "type": email_type,
        "exp": datetime.now(timezone.utc) + timedelta(days=_TOKEN_TTL_DAYS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def parse_unsubscribe_token(token: str) -> tuple[int, str] | None:
    """(user_id, email_type) or None — never raises on garbage input."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
    if payload.get("scope") != "unsubscribe":
        return None
    email_type = payload.get("type")
    if email_type not in EMAIL_TYPES:
        return None
    try:
        return int(payload["sub"]), email_type
    except (KeyError, TypeError, ValueError):
        return None


class UnsubscribeRequest(BaseModel):
    token: str


class EmailPrefs(BaseModel):
    alerts: bool
    price_drops: bool
    digest: bool


class EmailPrefsPatch(BaseModel):
    alerts: bool | None = None
    price_drops: bool | None = None
    digest: bool | None = None


@router.post("/unsubscribe")
async def unsubscribe(body: UnsubscribeRequest) -> dict:
    """One-click unsubscribe — no auth; the signed token is the authority."""
    parsed = parse_unsubscribe_token(body.token)
    if parsed is None:
        raise HTTPException(status_code=400, detail="Invalid or expired link.")
    user_id, email_type = parsed
    async with get_async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=400, detail="Invalid or expired link.")
        setattr(user, _PREF_FIELD[email_type], False)
        await session.commit()
    logger.info("unsubscribe: user=%d type=%s", user_id, email_type)
    return {"ok": True, "type": email_type}


@router.get("/prefs", response_model=EmailPrefs)
async def get_prefs(current_user: User = Depends(get_current_user)) -> EmailPrefs:
    async with get_async_session() as session:
        user = await session.get(User, current_user.id)
        return EmailPrefs(
            alerts=bool(user.email_alerts),
            price_drops=bool(user.email_price_drops),
            digest=bool(user.email_digest),
        )


@router.patch("/prefs", response_model=EmailPrefs)
async def patch_prefs(
    body: EmailPrefsPatch,
    current_user: User = Depends(get_current_user),
) -> EmailPrefs:
    async with get_async_session() as session:
        user = await session.get(User, current_user.id)
        if body.alerts is not None:
            user.email_alerts = body.alerts
        if body.price_drops is not None:
            user.email_price_drops = body.price_drops
        if body.digest is not None:
            user.email_digest = body.digest
        await session.commit()
        return EmailPrefs(
            alerts=user.email_alerts,
            price_drops=user.email_price_drops,
            digest=user.email_digest,
        )
