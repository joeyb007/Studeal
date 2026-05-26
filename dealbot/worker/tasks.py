from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select

from dealbot.agents.research import ResearchAgent
from dealbot.db.database import get_async_session
from dealbot.db.models import Watchlist
from dealbot.graph.graph import build_scorer_graph
from dealbot.llm.base import LLMClient
from dealbot.llm.groq_client import GroqClient
from dealbot.llm.ollama import OllamaClient
from dealbot.llm.openai_client import OpenAIClient
from dealbot.llm.vllm import vLLMClient
from dealbot.schemas import WatchlistContext
from dealbot.worker.celery_app import app

logger = logging.getLogger(__name__)


def _get_llm() -> LLMClient:
    backend = os.environ.get("LLM_BACKEND", "openai")
    if backend == "openai":
        return OpenAIClient()
    if backend == "groq":
        return GroqClient()
    if backend == "vllm":
        return vLLMClient()
    return OllamaClient()


@app.task(name="dealbot.worker.tasks.research_for_agent", bind=True, max_retries=3)
def research_for_agent(self, watchlist_id: int) -> dict:
    """Run the ResearchAgent for a single watchlist, then score+persist all deals."""
    try:
        llm = _get_llm()
        return asyncio.run(_run_research(llm, watchlist_id))
    except Exception as exc:
        logger.exception("research_for_agent failed for wl=%d: %s", watchlist_id, exc)
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 60)


async def _run_research(llm: LLMClient, watchlist_id: int) -> dict:
    # Load the watchlist context
    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
        if watchlist is None:
            logger.warning("research_for_agent: watchlist %d not found", watchlist_id)
            return {"watchlist_id": watchlist_id, "error": "not_found"}
        if not watchlist.context:
            logger.warning("research_for_agent: watchlist %d has no context", watchlist_id)
            return {"watchlist_id": watchlist_id, "error": "no_context"}
        context = WatchlistContext.model_validate_json(watchlist.context)

    # Run the agent (no event sink in this task — SSE is wired separately in Phase F)
    agent = ResearchAgent(llm=llm)
    state = await agent.run(watchlist_id=watchlist_id, context=context)

    logger.info(
        "research_for_agent: wl=%d turns=%d deals=%d cost=$%.4f reason=%s",
        watchlist_id, state.turns_used, state.deal_count,
        state.total_cost_usd, state.stop_reason,
    )

    # Fan out scoring + persistence for every accumulated deal.
    # Each branch returns an outcome dict; aggregate for per-hunt telemetry.
    outcomes: dict[str, int] = {
        "persisted_legitimate": 0,
        "persisted_rejected": 0,
        "dropped_resolution": 0,
        "errored": 0,
    }
    if state.deals_by_url:
        sem = asyncio.Semaphore(5)
        graph = build_scorer_graph(llm)

        async def _score_one(deal):
            async with sem:
                try:
                    result = await graph.ainvoke({"deal": deal})
                    outcome = (result or {}).get("outcome", "errored")
                    outcomes[outcome] = outcomes.get(outcome, 0) + 1
                except Exception:
                    logger.exception("scorer graph failed for %r", deal.title)
                    outcomes["errored"] += 1

        await asyncio.gather(*[_score_one(d) for d in state.deals_by_url.values()])

    seen = len(state.deals_by_url)
    visible = outcomes["persisted_legitimate"]
    dropped_total = (
        outcomes["persisted_rejected"]
        + outcomes["dropped_resolution"]
        + outcomes["errored"]
    )
    drop_rate = (dropped_total / seen * 100) if seen else 0.0
    logger.info(
        "research_for_agent: outcomes wl=%d seen=%d → visible=%d "
        "persisted_rejected=%d dropped_resolution=%d errored=%d (drop_rate=%.0f%%)",
        watchlist_id, seen, visible,
        outcomes["persisted_rejected"], outcomes["dropped_resolution"], outcomes["errored"],
        drop_rate,
    )

    return {
        "watchlist_id": watchlist_id,
        "turns_used": state.turns_used,
        "deal_count": state.deal_count,
        "cost_usd": state.total_cost_usd,
        "stop_reason": state.stop_reason,
    }


@app.task(name="dealbot.worker.tasks.daily_rehunt", bind=True, max_retries=3)
def daily_rehunt(self) -> dict:
    """Cron task: replay every HuntQuery row through the SearchRouter (no LLM)
    to refresh deals cheaply. Full ResearchAgent re-runs handled separately."""
    try:
        return asyncio.run(_run_daily_rehunt())
    except Exception as exc:
        logger.exception("daily_rehunt failed: %s", exc)
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 60)


async def _run_daily_rehunt() -> dict:
    from dealbot.db.models import HuntQuery
    from dealbot.graph.graph import build_scorer_graph
    from dealbot.search import SearchRouter, SearchResult

    router = SearchRouter()
    llm = _get_llm()
    scorer = build_scorer_graph(llm)
    sem = asyncio.Semaphore(3)
    stats = {"queries_replayed": 0, "deals_persisted": 0, "errors": 0}

    async with get_async_session() as session:
        now = datetime.now(timezone.utc)
        result = await session.execute(
            select(HuntQuery)
            .join(Watchlist, HuntQuery.watchlist_id == Watchlist.id)
            .where((Watchlist.expires_at == None) | (Watchlist.expires_at > now))  # noqa: E711
        )
        queries = list(result.scalars().all())

    logger.info("daily_rehunt: replaying %d queries", len(queries))

    async def _replay(hq):
        nonlocal stats
        async with sem:
            try:
                results, _cost = await router.search(hq.query_text, locale="ca")
                stats["queries_replayed"] += 1
                # Fan out scoring for each result
                from dealbot.graph.nodes import _result_to_deal_raw
                for r in results:
                    deal = _result_to_deal_raw(r, hq.query_text)
                    if deal:
                        try:
                            await scorer.ainvoke({"deal": deal})
                            stats["deals_persisted"] += 1
                        except Exception:
                            stats["errors"] += 1
            except Exception:
                logger.exception("daily_rehunt: replay failed for %r", hq.query_text)
                stats["errors"] += 1

    await asyncio.gather(*[_replay(q) for q in queries])

    logger.info("daily_rehunt complete: %s", stats)
    return stats
