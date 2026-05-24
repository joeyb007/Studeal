from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from dealbot.affiliates import rewrite as affiliate_rewrite
from dealbot.agents.scorer import ScorerAgent
from dealbot.db.database import get_async_session
from dealbot.db.models import Deal
from dealbot.db.semantic import retrieve_similar_deals
from dealbot.graph.state import PipelineState
from dealbot.llm.base import LLMClient
from dealbot.llm.embeddings import embed_text
from dealbot.schemas import Condition, DealRaw
from dealbot.search import SearchResult

def _similar_deals_context(similar: list[Deal]) -> str | None:
    """Format retrieved deals into a market context string for the scorer."""
    if not similar:
        return None
    lines = ["Market context — similar deals currently in catalog:"]
    for d in similar:
        if d.real_discount_pct and d.real_discount_pct > 0:
            price_info = (
                f"${d.sale_price:.2f} (was ${d.listed_price:.2f}, "
                f"{d.real_discount_pct:.0f}% off)"
            )
        else:
            price_info = f"${d.sale_price:.2f} (no discount)"
        lines.append(
            f"- {d.title}: {price_info} | score={d.score} | "
            f"{d.alert_tier} | {d.category} | {d.condition}"
        )
    lines.append(
        "Use these to benchmark: if this deal is priced lower or discounted more "
        "than similar items, score it higher."
    )
    return "\n".join(lines)


logger = logging.getLogger(__name__)


# --- Nodes ------------------------------------------------------------------


def _result_to_deal_raw(r: SearchResult, search_query: str) -> DealRaw | None:
    """Map a SearchResult to a DealRaw for downstream scoring/persistence.

    Skips results without a sale price — they aren't actionable deals.
    """
    if not r.url or not r.title:
        return None
    if r.sale_price is None or r.sale_price <= 0:
        return None

    listed = r.listed_price if r.listed_price and r.listed_price > r.sale_price else r.sale_price
    source_name = r.source_domain.replace("www.", "") if r.source_domain else r.provider

    return DealRaw(
        source=source_name,
        title=r.title,
        url=r.url,
        listed_price=listed,
        sale_price=r.sale_price,
        description=r.snippet[:500] if r.snippet else None,
        condition=Condition.unknown,
        source_type="api",
        search_query=search_query,
    )


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
    Validate a deal, embed it, then persist it to Postgres.

    Validation replaces the old scoring step — no RAG, no market context. Each
    deal is judged on its own merits. Rejected deals are still persisted with
    legitimate=false (filtered at view time).

    Used in the fan-out hunter graph where each branch handles a single candidate.
    Returns {} so no state is merged back to the shared graph.
    """
    deal = state["deal"]
    logger.info("score_and_persist_node: validating '%s'", deal.title)

    try:
        deal_text = f"{deal.title} {deal.description or ''}".strip()
        embedding = await embed_text(deal_text) or None

        scorer = ScorerAgent(llm=llm)
        validation = await scorer.validate(deal)
        logger.info(
            "score_and_persist_node: legitimate=%s confidence=%.2f reason=%r",
            validation.legitimate, validation.validation_confidence, validation.validation_reason[:80],
        )
    except Exception:
        logger.exception("score_and_persist_node: validation failed for '%s'", deal.title)
        return {}

    now = datetime.now(timezone.utc)
    affiliate_url = affiliate_rewrite(deal.url)
    # score + alert_tier still populated for back-compat until 0019 drops the columns
    legacy_score = 50 if validation.legitimate else 0
    legacy_tier = "digest" if validation.legitimate else "none"

    values = dict(
        title=deal.title,
        source=deal.source,
        url=deal.url,
        affiliate_url=affiliate_url if affiliate_url != deal.url else None,
        listed_price=deal.listed_price,
        sale_price=deal.sale_price,
        asin=deal.asin,
        score=legacy_score,
        alert_tier=legacy_tier,
        category=validation.category.value,
        tags=json.dumps(validation.tags),
        confidence="high" if validation.validation_confidence >= 0.7 else "low",
        real_discount_pct=validation.real_discount_pct,
        student_eligible=validation.student_eligible or deal.student_eligible,
        condition=validation.condition.value,
        embedding=embedding,
        legitimate=validation.legitimate,
        validation_confidence=validation.validation_confidence,
        validation_reason=validation.validation_reason,
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
                        "legitimate": values["legitimate"],
                        "validation_confidence": values["validation_confidence"],
                        "validation_reason": values["validation_reason"],
                        "scraped_at": values["scraped_at"],
                    },
                )
                .returning(Deal.id)
            )
            result = await session.execute(stmt)
            deal_id = result.scalar_one()
            await session.commit()
        logger.info(
            "score_and_persist_node: upserted '%s' id=%d legitimate=%s",
            deal.title, deal_id, result.legitimate,
        )
    except Exception as exc:
        logger.exception("score_and_persist_node: persist failed for '%s'", deal.title)

    return {}
