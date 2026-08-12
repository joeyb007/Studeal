from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi import Depends
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from dealbot.api.auth import create_access_token, get_current_user, hash_password, verify_password
from dealbot.api.limiter import limiter
from dealbot.db.database import get_async_session
from dealbot.db.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int


class GoogleCallbackRequest(BaseModel):
    google_id: str
    email: EmailStr
    name: str | None = None


@router.post("/register", status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def register(request: Request, body: RegisterRequest) -> dict[str, str]:
    async with get_async_session() as session:
        try:
            user = User(email=body.email, hashed_password=hash_password(body.password))
            session.add(user)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(status_code=409, detail="Email already registered")
    return {"detail": "Account created"}


@router.post("/token", response_model=TokenResponse)
@limiter.limit("10/minute")
async def login(request: Request, form: OAuth2PasswordRequestForm = Depends()) -> TokenResponse:
    async with get_async_session() as session:
        result = await session.execute(select(User).where(User.email == form.username))
        user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.hashed_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This account uses Google sign-in. Please continue with Google.",
        )
    if not verify_password(form.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return TokenResponse(access_token=create_access_token(user.id), user_id=user.id)


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)) -> dict:
    return {
        "id": current_user.id,
        "email": current_user.email,
        "is_pro": current_user.is_pro,
        # Google-only accounts have no password to change/verify.
        "has_password": bool(current_user.hashed_password),
    }


@router.post("/google", response_model=TokenResponse)
@limiter.limit("10/minute")
async def google_callback(request: Request, body: GoogleCallbackRequest) -> TokenResponse:
    async with get_async_session() as session:
        # Find existing user by google_id or email
        result = await session.execute(
            select(User).where(
                (User.google_id == body.google_id) | (User.email == body.email)
            )
        )
        user = result.scalar_one_or_none()

        if user is None:
            user = User(
                email=body.email,
                hashed_password="",  # Google users have no password
                google_id=body.google_id,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
        elif user.google_id is None:
            # Existing email/password user connecting Google for the first time
            user.google_id = body.google_id
            await session.commit()

    return TokenResponse(access_token=create_access_token(user.id), user_id=user.id)


# ---------------------------------------------------------------------------
# Password reset (2026-08-12 spec, stage 2): stateless signed tokens, 30 min.
# The token embeds a fingerprint of the CURRENT password hash, so using it
# (or any other password change) invalidates every outstanding link.
# ---------------------------------------------------------------------------

import hashlib
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from dealbot.api.auth import ALGORITHM, SECRET_KEY

_RESET_TOKEN_TTL_MIN = 30


def _password_fingerprint(hashed_password: str) -> str:
    return hashlib.sha256(hashed_password.encode()).hexdigest()[:12]


def make_reset_token(user: User) -> str:
    return jwt.encode(
        {
            "sub": str(user.id),
            "scope": "pw_reset",
            "pwf": _password_fingerprint(user.hashed_password),
            "exp": datetime.now(timezone.utc) + timedelta(minutes=_RESET_TOKEN_TTL_MIN),
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def parse_reset_token(token: str) -> tuple[int, str] | None:
    """(user_id, password_fingerprint) or None; never raises."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
    if payload.get("scope") != "pw_reset":
        return None
    try:
        return int(payload["sub"]), str(payload["pwf"])
    except (KeyError, TypeError, ValueError):
        return None


class ResetRequest(BaseModel):
    email: EmailStr


class ResetConfirm(BaseModel):
    token: str
    new_password: str


class ChangePassword(BaseModel):
    current_password: str
    new_password: str


class DeleteAccount(BaseModel):
    password: str | None = None    # required for credentials accounts


def _app_url() -> str:
    import os

    return os.environ.get("APP_BASE_URL", "https://studeal.site").rstrip("/")


@router.post("/reset-request")
@limiter.limit("3/minute")
async def reset_request(request: Request, body: ResetRequest) -> dict[str, str]:
    """Always 200 — existence of an account is never disclosed."""
    from dealbot.notifications.email import (
        build_google_account_notice_email,
        build_password_reset_email,
        send_email,
    )

    async with get_async_session() as session:
        result = await session.execute(select(User).where(User.email == body.email))
        user = result.scalar_one_or_none()

    if user is not None:
        if not user.hashed_password:      # Google-only account
            subject, text, html = build_google_account_notice_email()
        else:
            reset_url = f"{_app_url()}/reset-password?token={make_reset_token(user)}"
            subject, text, html = build_password_reset_email(reset_url)
        await send_email(user.email, subject, text, html=html)

    return {"detail": "If that email has an account, a reset link is on its way."}


@router.post("/reset-confirm")
@limiter.limit("5/minute")
async def reset_confirm(request: Request, body: ResetConfirm) -> dict[str, str]:
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    parsed = parse_reset_token(body.token)
    if parsed is None:
        raise HTTPException(status_code=400, detail="Invalid or expired link.")
    user_id, fingerprint = parsed
    async with get_async_session() as session:
        user = await session.get(User, user_id)
        if user is None or _password_fingerprint(user.hashed_password) != fingerprint:
            # Password changed since the link was minted — link is dead.
            raise HTTPException(status_code=400, detail="Invalid or expired link.")
        user.hashed_password = hash_password(body.new_password)
        await session.commit()
    return {"detail": "Password updated. You can sign in now."}


@router.post("/change-password")
async def change_password(
    body: ChangePassword,
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    async with get_async_session() as session:
        user = await session.get(User, current_user.id)
        if not user.hashed_password:
            raise HTTPException(status_code=400, detail="This account signs in with Google.")
        if not verify_password(body.current_password, user.hashed_password):
            raise HTTPException(status_code=401, detail="Current password is incorrect.")
        user.hashed_password = hash_password(body.new_password)
        await session.commit()
    return {"detail": "Password updated."}


@router.post("/delete-account")
async def delete_account(
    body: DeleteAccount,
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """Full deletion: verify identity, cancel any Stripe subscription, then
    delete the user row (FK cascades take watchlists, alerts, threads,
    watches). Refuses to orphan an active subscription."""
    async with get_async_session() as session:
        user = await session.get(User, current_user.id)
        if user.hashed_password:
            if not body.password or not verify_password(body.password, user.hashed_password):
                raise HTTPException(status_code=401, detail="Password is incorrect.")

        if user.stripe_subscription_id:
            import stripe

            try:
                stripe.Subscription.cancel(user.stripe_subscription_id)
            except stripe.StripeError:
                raise HTTPException(
                    status_code=502,
                    detail="Couldn't cancel your subscription. Try again, or cancel it from Manage billing first.",
                )

        # SQL-level delete: the ON DELETE CASCADE FKs own the cleanup
        # (watchlists, alerts, threads, watches). ORM-level delete would try
        # to null child FKs instead.
        from sqlalchemy import delete as sa_delete

        await session.execute(sa_delete(User).where(User.id == user.id))
        await session.commit()
    return {"detail": "Account deleted."}
