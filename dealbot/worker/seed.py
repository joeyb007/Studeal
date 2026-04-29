from __future__ import annotations

import asyncio
import logging
import os

from dealbot.graph.graph import build_hunter_graph
from dealbot.llm.base import LLMClient
from dealbot.llm.groq_client import GroqClient
from dealbot.llm.ollama import OllamaClient
from dealbot.llm.openai_client import OpenAIClient
from dealbot.llm.vllm import vLLMClient
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


async def _run_seed(llm: LLMClient) -> dict:
    graph = build_hunter_graph(llm)
    results = {"processed": 0, "skipped": 0, "errors": 0}

    for query in SEED_QUERIES:
        try:
            final_state = await graph.ainvoke({"keyword": query})
            if final_state.get("keyword_covered"):
                results["skipped"] += 1
            else:
                results["processed"] += 1
        except Exception:
            logger.exception("seed_deals: unhandled error for query '%s'", query)
            results["errors"] += 1

    logger.info("seed_deals: done — %s", results)
    return results
