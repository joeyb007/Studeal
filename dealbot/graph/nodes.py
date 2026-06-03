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
from dealbot.graph.state import PipelineState
from dealbot.llm.base import LLMClient
from dealbot.llm.embeddings import embed_text
from dealbot.scorer import compute_deal_score
from dealbot.schemas import Condition, DealRaw
from dealbot.search import SearchResult
from dealbot.search.google_resolver import GoogleShoppingResolver

# Module-level singleton — internal Semaphore(3) caps concurrent Google calls
# across all fan-out branches that hit this node in parallel.
_resolver = GoogleShoppingResolver()

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


async def score_and_persist_node(state: PipelineState, llm: LLMClient) -> dict:
    """
    Resolve, validate, embed, and persist a deal.

    Pipeline per deal:
      1. RESOLVE (Serper-sourced deals only): follow the Google aggregator URL
         to find the direct retailer link + real listed_price. Drop if no
         direct URL can be extracted — users shouldn't see deals they can't get.
      2. VALIDATE: LLM judges legitimacy (scams, parts-only, counterfeits). Rejected
         deals are still persisted with legitimate=false (filtered at view time).
      3. EMBED + PERSIST.

    Returns one of:
      {"outcome": "persisted_legitimate", "deal_id": int}
      {"outcome": "persisted_rejected",   "deal_id": int}
      {"outcome": "dropped_resolution",   "reason": str}
      {"outcome": "errored",              "reason": str}
    Aggregated by the caller in tasks.py for per-hunt telemetry.
    """
    deal = state["deal"]
    logger.info("score_and_persist_node: processing '%s'", deal.title)

    # Stage 1: link resolution gate (Serper aggregator URLs only)
    if deal.url and "google.com/search" in deal.url:
        try:
            offer = await _resolver.resolve(deal.url)
        except Exception as exc:
            logger.warning("score_and_persist_node: resolver crashed for %r: %s", deal.title, exc)
            return {"outcome": "dropped_resolution", "reason": "resolver_exception"}

        if not offer.direct_url:
            logger.info(
                "score_and_persist_node: DROPPING %r — no direct URL (%s)",
                deal.title, offer.failure_reason,
            )
            return {
                "outcome": "dropped_resolution",
                "reason": offer.failure_reason or "no_direct_url",
            }

        # Resolved — replace Serper's placeholder data with the real thing
        deal.url = offer.direct_url
        if offer.listed_price is not None:
            deal.listed_price = offer.listed_price
        if offer.sale_price is not None:
            deal.sale_price = offer.sale_price
        # Resolver may detect condition (refurb/used) from aria-label
        if offer.condition and deal.condition == "unknown":
            from dealbot.schemas import Condition
            _cond_map = {
                "refurbished": Condition.refurb,
                "certified refurbished": Condition.refurb,
                "renewed": Condition.refurb,
                "open box": Condition.refurb,
                "pre-owned": Condition.used,
                "used": Condition.used,
                "as is": Condition.used,
            }
            mapped = _cond_map.get(offer.condition.lower())
            if mapped:
                deal.condition = mapped
        logger.info(
            "score_and_persist_node: resolved %r → %s (listed=$%s disc=%s%% cond=%s)",
            deal.title, deal.url[:70],
            offer.listed_price, offer.real_discount_pct, offer.condition,
        )

    # Stage 2: validation
    try:
        deal_text = f"{deal.title} {deal.description or ''}".strip()
        embedding = await embed_text(deal_text) or None

        scorer = ScorerAgent(llm=llm)
        validation = await scorer.validate(deal)
        logger.info(
            "score_and_persist_node: legitimate=%s confidence=%.2f reason=%r",
            validation.legitimate, validation.validation_confidence, validation.validation_reason[:80],
        )
    except Exception as exc:
        logger.exception("score_and_persist_node: validation failed for '%s'", deal.title)
        return {"outcome": "errored", "reason": f"validation_error: {exc}"}

    now = datetime.now(timezone.utc)
    affiliate_url = affiliate_rewrite(deal.url)

    deal_score = compute_deal_score(
        discount_pct=validation.real_discount_pct,
        validation_confidence=validation.validation_confidence,
        condition=validation.condition.value,
        student_eligible=validation.student_eligible or deal.student_eligible,
        source=deal.source,
    ) if validation.legitimate else 0

    values = dict(
        title=deal.title,
        source=deal.source,
        url=deal.url,
        affiliate_url=affiliate_url if affiliate_url != deal.url else None,
        listed_price=deal.listed_price,
        sale_price=deal.sale_price,
        asin=deal.asin,
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
        deal_score=deal_score,
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
                        "real_discount_pct": values["real_discount_pct"],
                        "student_eligible": values["student_eligible"],
                        "condition": values["condition"],
                        "affiliate_url": values["affiliate_url"],
                        "embedding": values["embedding"],
                        "legitimate": values["legitimate"],
                        "validation_confidence": values["validation_confidence"],
                        "validation_reason": values["validation_reason"],
                        "deal_score": values["deal_score"],
                        "scraped_at": values["scraped_at"],
                    },
                )
                .returning(Deal.id)
            )
            sa_result = await session.execute(stmt)
            deal_id = sa_result.scalar_one()
            await session.commit()
        logger.info(
            "score_and_persist_node: upserted '%s' id=%d legitimate=%s",
            deal.title, deal_id, validation.legitimate,
        )
    except Exception as exc:
        logger.exception("score_and_persist_node: persist failed for '%s'", deal.title)
        return {"outcome": "errored", "reason": f"persist_failed: {exc}"}

    return {
        "outcome": "persisted_legitimate" if validation.legitimate else "persisted_rejected",
        "deal_id": deal_id,
    }
