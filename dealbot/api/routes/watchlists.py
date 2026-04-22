from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, model_validator
from sqlalchemy import func, select

from dealbot.agents.keyword_extractor import extract_keywords
from dealbot.api.auth import get_current_user
from dealbot.db.database import get_async_session
from dealbot.db.models import User, Watchlist, WatchlistKeyword
from dealbot.llm.base import LLMClient
from dealbot.llm.embeddings import embed_text
from dealbot.llm.ollama import OllamaClient
from dealbot.llm.vllm import vLLMClient

router = APIRouter(prefix="/watchlists", tags=["watchlists"])

WATCHLIST_TTL_DAYS = 60  # inactive watchlists expire after 60 days
FREE_WATCHLIST_CAP = 1
PRO_WATCHLIST_CAP = 5


def _get_llm() -> LLMClient:
    return vLLMClient() if os.environ.get("LLM_BACKEND") == "vllm" else OllamaClient()


def _expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=WATCHLIST_TTL_DAYS)


class WatchlistCreate(BaseModel):
    name: str
    description: str | None = None   # natural language — LLM extracts keywords
    keywords: list[str] = []         # explicit keywords (used if description is absent)
    min_score: int = 50
    alert_tier_threshold: str = "digest"

    @model_validator(mode="after")
    def require_description_or_keywords(self) -> "WatchlistCreate":
        if not self.description and not self.keywords:
            raise ValueError("Provide either a description or at least one keyword.")
        return self


class WatchlistResponse(BaseModel):
    id: int
    name: str
    keywords: list[str]
    min_score: int
    alert_tier_threshold: str
    expires_at: Optional[str]


@router.post("", response_model=WatchlistResponse, status_code=status.HTTP_201_CREATED)
async def create_watchlist(
    body: WatchlistCreate,
    current_user: User = Depends(get_current_user),
) -> WatchlistResponse:
    async with get_async_session() as session:
        now = datetime.now(timezone.utc)
        count_result = await session.execute(
            select(func.count(Watchlist.id)).where(
                Watchlist.user_id == current_user.id,
                (Watchlist.expires_at == None) | (Watchlist.expires_at > now),  # noqa: E711
            )
        )
        cap = PRO_WATCHLIST_CAP if current_user.is_pro else FREE_WATCHLIST_CAP
        if count_result.scalar_one() >= cap:
            detail = (
                f"Pro members can have up to {PRO_WATCHLIST_CAP} active watchlists. Delete one to create a new one."
                if current_user.is_pro
                else f"Free accounts are limited to {FREE_WATCHLIST_CAP} watchlist. Upgrade to pro for up to {PRO_WATCHLIST_CAP}."
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)

        if body.description and not body.keywords:
            keywords = await extract_keywords(body.description, _get_llm())
        else:
            keywords = [kw.lower().strip() for kw in body.keywords]

        watchlist = Watchlist(
            user_id=current_user.id,
            name=body.name,
            min_score=body.min_score,
            alert_tier_threshold=body.alert_tier_threshold,
            expires_at=_expiry(),
        )
        session.add(watchlist)
        await session.flush()

        for kw in keywords:
            embedding = await embed_text(kw)
            session.add(WatchlistKeyword(
                watchlist_id=watchlist.id,
                keyword=kw,
                embedding=embedding or None,
            ))

        await session.commit()
        await session.refresh(watchlist)

    return WatchlistResponse(
        id=watchlist.id,
        name=watchlist.name,
        keywords=keywords,
        min_score=watchlist.min_score,
        alert_tier_threshold=watchlist.alert_tier_threshold,
        expires_at=watchlist.expires_at.isoformat() if watchlist.expires_at else None,
    )


@router.delete("/{watchlist_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_watchlist(
    watchlist_id: int,
    current_user: User = Depends(get_current_user),
) -> None:
    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
        if watchlist is None or watchlist.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Watchlist not found.")
        await session.delete(watchlist)
        await session.commit()


@router.get("", response_model=list[WatchlistResponse])
async def list_watchlists(
    current_user: User = Depends(get_current_user),
) -> list[WatchlistResponse]:
    now = datetime.now(timezone.utc)
    async with get_async_session() as session:
        result = await session.execute(
            select(Watchlist).where(
                Watchlist.user_id == current_user.id,
                (Watchlist.expires_at == None) | (Watchlist.expires_at > now),  # noqa: E711
            )
        )
        watchlists = result.scalars().all()

        responses = []
        for wl in watchlists:
            # Refresh expiry on activity — keeps active users' watchlists alive
            wl.expires_at = _expiry()

            kw_result = await session.execute(
                select(WatchlistKeyword).where(WatchlistKeyword.watchlist_id == wl.id)
            )
            keywords = [k.keyword for k in kw_result.scalars().all()]
            responses.append(WatchlistResponse(
                id=wl.id,
                name=wl.name,
                keywords=keywords,
                min_score=wl.min_score,
                alert_tier_threshold=wl.alert_tier_threshold,
                expires_at=wl.expires_at.isoformat() if wl.expires_at else None,
            ))

        await session.commit()

    return responses
