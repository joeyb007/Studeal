from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from dealbot.affiliates import rewrite as affiliate_rewrite
from dealbot.agents.orchestrator import OrchestratorAgent
from dealbot.agents.scorer import ScorerAgent
from dealbot.db.database import get_async_session
from dealbot.db.models import Deal
from dealbot.db.rag import keyword_covered_today, retrieve_similar_deals
from dealbot.graph.state import PipelineState
from dealbot.llm.base import LLMClient
from dealbot.llm.embeddings import embed_text

def _similar_deals_context(similar: list[Deal]) -> str | None:
    """Format retrieved deals into a context string for the scorer's system prompt."""
    if not similar:
        return None
    lines = ["Similar deals scored previously (use for market context):"]
    for d in similar:
        lines.append(
            f"- {d.title}: score={d.score}, tier={d.alert_tier}, "
            f"category={d.category}, sale_price=${d.sale_price:.2f}"
        )
    return "\n".join(lines)


logger = logging.getLogger(__name__)


# --- Nodes ------------------------------------------------------------------

async def keyword_dedup_node(state: PipelineState) -> PipelineState:
    """Skip the hunt if a semantically similar keyword was already searched today."""
    keyword = state.get("keyword", "")
    embedding = await embed_text(keyword)
    if not embedding:
        return {**state, "keyword_covered": False}

    async with get_async_session() as session:
        covered = await keyword_covered_today(embedding, session)

    if covered:
        logger.info("keyword_dedup_node: '%s' already covered today, skipping", keyword)
    return {**state, "keyword_covered": covered}


async def orchestrator_node(state: PipelineState, llm: LLMClient) -> PipelineState:
    """Run the orchestrator: parallel BrowserAgent sessions → deduplicated candidates."""
    keyword = state["keyword"]
    logger.info("orchestrator_node: starting for keyword=%r", keyword)
    agent = OrchestratorAgent(llm=llm)
    candidates = await agent.run(keyword)
    logger.info("orchestrator_node: %d candidates", len(candidates))
    return {**state, "candidates": candidates}


async def ingest_node(state: PipelineState) -> PipelineState:
    """
    Validates the incoming DealRaw and passes it through.
    In a later phase this will pull from a Redis stream instead.
    """
    logger.info("ingest_node: deal=%s source=%s", state["deal"].title, state["deal"].source)
    return state


async def score_node(state: PipelineState, llm: LLMClient) -> PipelineState:
    """Embeds the deal, retrieves similar deals via RAG, then runs ScorerAgent."""
    deal = state["deal"]
    logger.info("score_node: scoring '%s'", deal.title)

    try:
        # 1. Generate embedding for this deal
        deal_text = f"{deal.title} {deal.description or ''}".strip()
        embedding = await embed_text(deal_text)

        # 2. RAG: retrieve similar historical deals
        similar: list[Deal] = []
        if embedding:
            async with get_async_session() as session:
                similar = await retrieve_similar_deals(embedding, session)
            logger.debug("score_node: retrieved %d similar deals", len(similar))

        # 3. Score with context
        scorer = ScorerAgent(llm=llm)
        score_result = await scorer.score(
            deal,
            similar_context=_similar_deals_context(similar),
        )
        logger.info(
            "score_node: score=%d tier=%s confidence=%s",
            score_result.score,
            score_result.alert_tier,
            score_result.confidence,
        )
        return {"score_result": score_result, "embedding": embedding}
    except Exception as exc:
        logger.exception("score_node: failed to score deal '%s'", deal.title)
        return {"error": str(exc)}


async def persist_node(state: PipelineState) -> PipelineState:
    """Writes DealScore + embedding to Postgres via SQLAlchemy. Skipped silently if error is set."""
    if "error" in state:
        logger.warning("persist_node: skipping due to upstream error: %s", state["error"])
        return state

    score_result = state.get("score_result")
    if score_result is None:
        logger.warning("persist_node: no score_result in state, skipping")
        return state

    deal = score_result.deal
    embedding = state.get("embedding") or None

    now = datetime.now(timezone.utc)
    values = dict(
        title=deal.title,
        source=deal.source,
        url=deal.url,
        listed_price=deal.listed_price,
        sale_price=deal.sale_price,
        asin=deal.asin,
        score=score_result.score,
        alert_tier=score_result.alert_tier.value,
        category=score_result.category.value,
        tags=json.dumps(score_result.tags),
        confidence=score_result.confidence,
        real_discount_pct=score_result.real_discount_pct,
        student_eligible=deal.student_eligible,
        condition=score_result.condition.value,
        embedding=embedding,
        hunt_date=date.today(),
        first_seen_at=now,  # only written on INSERT; excluded from DO UPDATE
        scraped_at=now,
    )

    async with get_async_session() as session:
        stmt = (
            pg_insert(Deal)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["url"],
                set_={
                    "title": values["title"],
                    "listed_price": values["listed_price"],
                    "sale_price": values["sale_price"],
                    "score": values["score"],
                    "alert_tier": values["alert_tier"],
                    "real_discount_pct": values["real_discount_pct"],
                    "student_eligible": values["student_eligible"],
                    "condition": values["condition"],
                    "embedding": values["embedding"],
                    "scraped_at": values["scraped_at"],
                    # first_seen_at intentionally omitted — never overwritten on re-scrape
                },
            )
            .returning(Deal.id)
        )
        result = await session.execute(stmt)
        deal_id = result.scalar_one()
        await session.commit()
        logger.info("persist_node: upserted deal '%s' id=%d score=%d", deal.title, deal_id, score_result.score)
    return {}


async def score_and_persist_node(state: PipelineState, llm: LLMClient) -> dict:
    """
    Score a deal then immediately persist it to Postgres.

    Used in the fan-out hunter graph where each branch handles a single candidate.
    Returns {} so no state is merged back to the shared graph — avoids
    InvalidUpdateError when many branches complete simultaneously.
    """
    deal = state["deal"]
    logger.info("score_and_persist_node: scoring '%s'", deal.title)

    try:
        deal_text = f"{deal.title} {deal.description or ''}".strip()
        embedding = await embed_text(deal_text) or None

        similar: list[Deal] = []
        if embedding:
            async with get_async_session() as session:
                similar = await retrieve_similar_deals(embedding, session)

        scorer = ScorerAgent(llm=llm)
        score_result = await scorer.score(deal, similar_context=_similar_deals_context(similar))
        logger.info(
            "score_and_persist_node: score=%d tier=%s",
            score_result.score, score_result.alert_tier,
        )
    except Exception as exc:
        logger.exception("score_and_persist_node: scoring failed for '%s'", deal.title)
        return {}

    now = datetime.now(timezone.utc)
    affiliate_url = affiliate_rewrite(deal.url)
    values = dict(
        title=deal.title,
        source=deal.source,
        url=deal.url,
        affiliate_url=affiliate_url if affiliate_url != deal.url else None,
        listed_price=deal.listed_price,
        sale_price=deal.sale_price,
        asin=deal.asin,
        score=score_result.score,
        alert_tier=score_result.alert_tier.value,
        category=score_result.category.value,
        tags=json.dumps(score_result.tags),
        confidence=score_result.confidence,
        real_discount_pct=score_result.real_discount_pct,
        student_eligible=deal.student_eligible,
        condition=score_result.condition.value,
        embedding=embedding,
        hunt_date=date.today(),
        first_seen_at=now,
        scraped_at=now,
    )

    try:
        async with get_async_session() as session:
            stmt = (
                pg_insert(Deal)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["url"],
                    set_={
                        "title": values["title"],
                        "listed_price": values["listed_price"],
                        "sale_price": values["sale_price"],
                        "score": values["score"],
                        "alert_tier": values["alert_tier"],
                        "real_discount_pct": values["real_discount_pct"],
                        "student_eligible": values["student_eligible"],
                        "condition": values["condition"],
                        "affiliate_url": values["affiliate_url"],
                        "embedding": values["embedding"],
                        "scraped_at": values["scraped_at"],
                    },
                )
                .returning(Deal.id)
            )
            result = await session.execute(stmt)
            deal_id = result.scalar_one()
            await session.commit()
        logger.info(
            "score_and_persist_node: upserted '%s' id=%d score=%d",
            deal.title, deal_id, score_result.score,
        )
    except Exception as exc:
        logger.exception("score_and_persist_node: persist failed for '%s'", deal.title)

    return {}
