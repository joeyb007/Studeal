from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from dealbot.agents.scorer import ScorerAgent
from dealbot.db.database import get_async_session
from dealbot.db.models import Deal
from dealbot.graph.state import PipelineState
from dealbot.llm.base import LLMClient
from dealbot.worker.matching import run_matching

# Replace with your Amazon Associates tag at deploy time
AMAZON_AFFILIATE_TAG = "dealbot-20"


def _affiliate_url(url: str, asin: str | None) -> str:
    """Rewrite Amazon product URLs to include the affiliate tag."""
    if asin:
        return f"https://www.amazon.com/dp/{asin}?tag={AMAZON_AFFILIATE_TAG}"
    return url

logger = logging.getLogger(__name__)


# --- Nodes ------------------------------------------------------------------

async def ingest_node(state: PipelineState) -> PipelineState:
    """
    Validates the incoming DealRaw and passes it through.
    In a later phase this will pull from a Redis stream instead.
    """
    logger.info("ingest_node: deal=%s source=%s", state["deal"].title, state["deal"].source)
    return state


async def score_node(state: PipelineState, llm: LLMClient) -> PipelineState:
    """Runs ScorerAgent and writes the result into state."""
    deal = state["deal"]
    logger.info("score_node: scoring '%s'", deal.title)

    try:
        scorer = ScorerAgent(llm=llm)
        score_result = await scorer.score(deal)
        logger.info(
            "score_node: score=%d tier=%s confidence=%s",
            score_result.score,
            score_result.alert_tier,
            score_result.confidence,
        )
        return {**state, "score_result": score_result}
    except Exception as exc:
        logger.exception("score_node: failed to score deal '%s'", deal.title)
        return {**state, "error": str(exc)}


async def persist_node(state: PipelineState) -> PipelineState:
    """Writes DealScore to Postgres via SQLAlchemy. Skipped silently if error is set."""
    if "error" in state:
        logger.warning("persist_node: skipping due to upstream error: %s", state["error"])
        return state

    score_result = state.get("score_result")
    if score_result is None:
        logger.warning("persist_node: no score_result in state, skipping")
        return state

    deal = score_result.deal
    values = dict(
        title=deal.title,
        source=deal.source,
        url=_affiliate_url(deal.url, deal.asin),
        listed_price=deal.listed_price,
        sale_price=deal.sale_price,
        asin=deal.asin,
        score=score_result.score,
        alert_tier=score_result.alert_tier.value,
        category=score_result.category,
        tags=json.dumps(score_result.tags),
        confidence=score_result.confidence,
        real_discount_pct=score_result.real_discount_pct,
        scraped_at=datetime.now(timezone.utc),
    )

    async with get_async_session() as session:
        try:
            row = Deal(**values)
            session.add(row)
            await session.flush()  # get row.id without closing the session
            await run_matching(row, session)
            await session.commit()
            logger.info("persist_node: saved deal '%s' with score %d", deal.title, score_result.score)
        except IntegrityError:
            await session.rollback()
            logger.info("persist_node: duplicate skipped '%s'", deal.url)
    return state
