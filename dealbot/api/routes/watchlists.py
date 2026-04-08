from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, model_validator
from sqlalchemy import select

from dealbot.agents.keyword_extractor import extract_keywords
from dealbot.api.auth import get_current_user
from dealbot.db.database import get_async_session
from dealbot.db.models import User, Watchlist, WatchlistKeyword
from dealbot.llm.base import LLMClient
from dealbot.llm.embeddings import embed_text
from dealbot.llm.ollama import OllamaClient
from dealbot.llm.vllm import vLLMClient

router = APIRouter(prefix="/watchlists", tags=["watchlists"])


def _get_llm() -> LLMClient:
    return vLLMClient() if os.environ.get("LLM_BACKEND") == "vllm" else OllamaClient()


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


@router.post("", response_model=WatchlistResponse, status_code=status.HTTP_201_CREATED)
async def create_watchlist(
    body: WatchlistCreate,
    current_user: User = Depends(get_current_user),
) -> WatchlistResponse:
    # Resolve keywords — extract from description if not explicitly provided
    if body.description and not body.keywords:
        keywords = await extract_keywords(body.description, _get_llm())
    else:
        keywords = [kw.lower().strip() for kw in body.keywords]

    async with get_async_session() as session:
        watchlist = Watchlist(
            user_id=current_user.id,
            name=body.name,
            min_score=body.min_score,
            alert_tier_threshold=body.alert_tier_threshold,
        )
        session.add(watchlist)
        await session.flush()  # get watchlist.id before adding keywords

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
    )


@router.get("", response_model=list[WatchlistResponse])
async def list_watchlists(
    current_user: User = Depends(get_current_user),
) -> list[WatchlistResponse]:
    async with get_async_session() as session:
        result = await session.execute(
            select(Watchlist).where(Watchlist.user_id == current_user.id)
        )
        watchlists = result.scalars().all()

        responses = []
        for wl in watchlists:
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
            ))

    return responses
