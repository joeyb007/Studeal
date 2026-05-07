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


@app.task(name="dealbot.worker.tasks.hunt_keyword", bind=True, max_retries=3)
def hunt_keyword(self, keyword: str) -> dict:
    """
    Celery task: run the hunter pipeline for a single keyword.
    Dispatched immediately when a watchlist is created.
    """
    try:
        llm = _get_llm()
        return asyncio.run(_run_single(llm, keyword))
    except Exception as exc:
        logger.exception("hunt_keyword task failed for '%s': %s", keyword, exc)
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 60)


async def _run_single(llm: LLMClient, keyword: str) -> dict:
    graph = build_hunter_graph(llm)
    try:
        final_state = await graph.ainvoke({"keyword": keyword})
        skipped = bool(final_state.get("keyword_covered"))
        logger.info("hunt_keyword: keyword=%r skipped=%s", keyword, skipped)
        return {"keyword": keyword, "skipped": skipped}
    except Exception:
        logger.exception("hunt_keyword: unhandled error for keyword '%s'", keyword)
        return {"keyword": keyword, "error": True}


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
    sem = asyncio.Semaphore(3)

    async with get_async_session() as session:
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
    results: dict[str, int] = {"processed": 0, "skipped": 0, "errors": 0}
    lock = asyncio.Lock()

    async def _hunt_one(kw: WatchlistKeyword) -> None:
        async with sem:
            try:
                final_state = await graph.ainvoke({"keyword": kw.keyword})
                async with lock:
                    if final_state.get("keyword_covered"):
                        results["skipped"] += 1
                    else:
                        results["processed"] += 1
            except Exception:
                logger.exception("hunt_deals: unhandled error for keyword '%s'", kw.keyword)
                async with lock:
                    results["errors"] += 1

    await asyncio.gather(*[_hunt_one(kw) for kw in keywords])

    total = len(keywords)
    logger.info(
        "hunt_deals complete: processed=%d skipped=%d errors=%d total=%d",
        results["processed"], results["skipped"], results["errors"], total,
    )
    if total > 0 and results["processed"] == 0 and results["errors"] > 0:
        logger.error(
            "hunt_deals: ZERO keywords produced deals and %d errored — "
            "pipeline may be failing",
            results["errors"],
        )
    return results
