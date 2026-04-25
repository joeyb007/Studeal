from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select

from dealbot.db.database import get_async_session
from dealbot.db.models import Watchlist, WatchlistKeyword
from dealbot.graph.graph import build_hunter_graph
from dealbot.llm.base import LLMClient
from dealbot.llm.groq_client import GroqClient
from dealbot.llm.ollama import OllamaClient
from dealbot.llm.openai_client import OpenAIClient
from dealbot.llm.vllm import vLLMClient
from dealbot.worker.celery_app import app

logger = logging.getLogger(__name__)


def _get_llm() -> LLMClient:
    backend = os.environ.get("LLM_BACKEND", "ollama")
    if backend == "openai":
        return OpenAIClient()
    if backend == "groq":
        return GroqClient()
    if backend == "vllm":
        return vLLMClient()
    return OllamaClient()


@app.task(name="dealbot.worker.tasks.hunt_deals", bind=True, max_retries=3)
def hunt_deals(self) -> dict:
    """
    Celery task: run the hunter pipeline for every watchlist keyword.
    Fires once daily via Celery Beat.
    """
    try:
        llm = _get_llm()
        return asyncio.run(_run_hunter(llm))
    except Exception as exc:
        logger.exception("hunt_deals task failed: %s", exc)
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 60)


async def _run_hunter(llm: LLMClient) -> dict:
    graph = build_hunter_graph(llm)

    async with get_async_session() as session:
        # Only hunt keywords from non-expired watchlists
        now = datetime.now(timezone.utc)
        result = await session.execute(
            select(WatchlistKeyword)
            .join(Watchlist, WatchlistKeyword.watchlist_id == Watchlist.id)
            .where(
                (Watchlist.expires_at == None) | (Watchlist.expires_at > now)  # noqa: E711
            )
        )
        keywords = result.scalars().all()

    logger.info("hunt_deals: %d keywords to process", len(keywords))
    results = {"processed": 0, "skipped": 0, "errors": 0}

    for kw in keywords:
        try:
            final_state = await graph.ainvoke({"keyword": kw.keyword})
            if final_state.get("keyword_covered"):
                results["skipped"] += 1
            else:
                results["processed"] += 1
        except Exception:
            logger.exception("hunt_deals: unhandled error for keyword '%s'", kw.keyword)
            results["errors"] += 1

    logger.info("hunt_deals: done — %s", results)
    return results
