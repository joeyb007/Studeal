from __future__ import annotations

import asyncio
import logging
import os

from dealbot.worker.celery_app import app
from dealbot.scrapers.base import BaseAdapter
from dealbot.scrapers.slickdeals import SlickdealsAdapter
from dealbot.graph.graph import build_graph
from dealbot.llm.base import LLMClient
from dealbot.llm.ollama import OllamaClient
from dealbot.llm.anthropic import AnthropicClient

logger = logging.getLogger(__name__)


@app.task(name="dealbot.worker.tasks.scrape_slickdeals", bind=True, max_retries=3)
def scrape_slickdeals(self) -> dict:
    """
    Celery task: fetch Slickdeals RSS, score each deal, persist to DB.
    Retries up to 3 times on failure with exponential backoff.
    """
    try:
        llm: LLMClient = (
            AnthropicClient() if os.environ.get("LLM_BACKEND") == "anthropic" else OllamaClient()
        )
        return asyncio.run(_run_pipeline(SlickdealsAdapter(), llm))
    except Exception as exc:
        logger.exception("scrape_slickdeals task failed: %s", exc)
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 60)


async def _run_pipeline(adapter: BaseAdapter, llm: LLMClient) -> dict:
    graph = build_graph(llm)

    deals = await adapter.fetch()
    logger.info("pipeline: fetched %d deals from %s", len(deals), type(adapter).__name__)

    results = {"scored": 0, "errors": 0}

    for deal in deals:
        try:
            final_state = await graph.ainvoke({"deal": deal})
            if "error" in final_state:
                logger.warning("pipeline error for '%s': %s", deal.title, final_state["error"])
                results["errors"] += 1
            else:
                results["scored"] += 1
        except Exception:
            logger.exception("unhandled error scoring deal '%s'", deal.title)
            results["errors"] += 1

    logger.info("pipeline: done — %s", results)
    return results
