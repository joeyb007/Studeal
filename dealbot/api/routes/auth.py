from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi import Depends
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from dealbot.api.auth import create_access_token, hash_password, verify_password
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
    return {"id": current_user.id, "email": current_user.email, "is_pro": current_user.is_pro}


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
