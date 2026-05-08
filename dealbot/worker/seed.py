from __future__ import annotations

import asyncio
import logging
import os

from dealbot.graph.graph import build_hunter_graph
from dealbot.graph.nodes import score_and_persist_node
from dealbot.llm.base import LLMClient
from dealbot.llm.groq_client import GroqClient
from dealbot.llm.ollama import OllamaClient
from dealbot.llm.openai_client import OpenAIClient
from dealbot.llm.vllm import vLLMClient
from dealbot.scrapers.community import (
    COMMUNITY_RSS_FEEDS,
    STUDENT_DEAL_SOURCES,
    fetch_rss_deals,
    fetch_site_deals,
)
from dealbot.worker.celery_app import app

logger = logging.getLogger(__name__)

# Editorial seed queries — broad, high-traffic categories students actually search.
# Runs once daily before the watchlist hunt so the catalog is pre-populated.
SEED_QUERIES = [
    # Tech essentials
    "laptop deals for college students",
    "noise cancelling headphones under $100",
    "monitor sale student setup",
    "mechanical keyboard cheap",
    # Dorm / first apartment
    "mini fridge deal",
    "bedding set sale twin XL dorm",
    "desk lamp cheap",
    "storage shelves affordable",
    "shower caddy dorm essentials",
    "microwave sale small apartment",
    "air purifier cheap",
    "window fan sale",
    # Kitchen starter
    "coffee maker cheap",
    "cookware set sale first apartment",
    "knife set affordable",
    "meal prep containers sale",
    "electric kettle deal",
    # Furniture on a budget
    "desk chair affordable student",
    "floor lamp cheap living room",
    "bookshelf sale",
    # Everyday carry / commute
    "backpack for college on sale",
    "water bottle insulated cheap",
    "bike lock sale",
    # Student-specific
    "student laptop deals canada",
    "apple education discount canada",
    "student discount electronics canada",
    "unidays canada deals",
]


def _get_llm() -> LLMClient:
    backend = os.environ.get("LLM_BACKEND", "ollama")
    if backend == "openai":
        return OpenAIClient()
    if backend == "groq":
        return GroqClient()
    if backend == "vllm":
        return vLLMClient()
    return OllamaClient()


@app.task(name="dealbot.worker.seed.seed_deals", bind=True, max_retries=3)
def seed_deals(self) -> dict:
    """
    Celery task: run the hunter pipeline for each editorial seed query.
    Fires once daily (before hunt_deals) to pre-populate the deal catalog.
    """
    try:
        llm = _get_llm()
        return asyncio.run(_run_seed(llm))
    except Exception as exc:
        logger.exception("seed_deals task failed: %s", exc)
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 60)


async def _run_community_sources(llm: LLMClient) -> dict:
    """Fetch RSS feeds and student deal sites, score and persist each deal found."""
    sem = asyncio.Semaphore(3)
    results: dict[str, int] = {"persisted": 0, "errors": 0}
    lock = asyncio.Lock()

    async def _process_source(coro) -> None:
        async with sem:
            try:
                deals = await coro
                for deal in deals:
                    try:
                        await score_and_persist_node({"deal": deal}, llm)
                        async with lock:
                            results["persisted"] += 1
                    except Exception:
                        logger.exception("community: failed to score/persist '%s'", deal.title)
                        async with lock:
                            results["errors"] += 1
            except Exception:
                logger.exception("community: source fetch failed")
                async with lock:
                    results["errors"] += 1

    tasks = (
        [_process_source(fetch_rss_deals(llm, url)) for url in COMMUNITY_RSS_FEEDS]
        + [_process_source(fetch_site_deals(llm, url)) for url in STUDENT_DEAL_SOURCES]
    )
    await asyncio.gather(*tasks)
    logger.info("community_sources: persisted=%d errors=%d", results["persisted"], results["errors"])
    return results


async def _run_seed(llm: LLMClient) -> dict:
    graph = build_hunter_graph(llm)
    sem = asyncio.Semaphore(3)
    results: dict[str, int] = {"processed": 0, "skipped": 0, "errors": 0}
    lock = asyncio.Lock()

    async def _hunt_one(query: str) -> None:
        async with sem:
            try:
                final_state = await graph.ainvoke({"keyword": query})
                async with lock:
                    if final_state.get("keyword_covered"):
                        results["skipped"] += 1
                    else:
                        results["processed"] += 1
            except Exception:
                logger.exception("seed_deals: unhandled error for query '%s'", query)
                async with lock:
                    results["errors"] += 1

    # Run seed queries and community sources in parallel
    await asyncio.gather(
        asyncio.gather(*[_hunt_one(q) for q in SEED_QUERIES]),
        _run_community_sources(llm),
    )

    total = len(SEED_QUERIES)
    logger.info(
        "seed_deals complete: processed=%d skipped=%d errors=%d total=%d",
        results["processed"], results["skipped"], results["errors"], total,
    )
    zero_yield = results["errors"] + results["skipped"]
    if results["processed"] == 0:
        logger.error(
            "seed_deals: ZERO deals produced across all %d queries — "
            "pipeline may be blocked (Browserbase/Google Shopping)",
            total,
        )
    elif zero_yield / total > 0.5:
        logger.warning(
            "seed_deals: low yield — %d/%d queries produced no new deals",
            zero_yield, total,
        )
    return results
