from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from dealbot.agents.composition import build_orchestrator_from_env
from dealbot.agents.state import DealOffer, OrchestratorState
from dealbot.db.database import get_async_session
from dealbot.db.models import Deal, Watchlist
from dealbot.schemas import WatchlistContext
from dealbot.worker.celery_app import app

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# research_for_agent — Phase 1.6 rewrite. Drives DealHuntOrchestrator end-to-
# end + persists DealOffers to the Deal table.
# ---------------------------------------------------------------------------

@app.task(name="dealbot.worker.tasks.research_for_agent", bind=True, max_retries=3)
def research_for_agent(self, watchlist_id: int) -> dict:
    """Run the autonomous browser agent for a single watchlist; persist offers."""
    try:
        return asyncio.run(_run_dealhunt(watchlist_id))
    except Exception as exc:
        logger.exception("research_for_agent failed for wl=%d: %s", watchlist_id, exc)
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 60)


async def _run_dealhunt(watchlist_id: int) -> dict:
    # 1. Load watchlist context.
    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
        if watchlist is None:
            logger.warning("research_for_agent: watchlist %d not found", watchlist_id)
            return {"watchlist_id": watchlist_id, "error": "not_found"}
        if not watchlist.context:
            logger.warning("research_for_agent: watchlist %d has no context", watchlist_id)
            return {"watchlist_id": watchlist_id, "error": "no_context"}
        context = WatchlistContext.model_validate_json(watchlist.context)

    # 2. Build + run orchestrator. AGENT_BROWSER_BACKEND env var picks
    # Browserbase (production) or local Playwright (dev/eval).
    orchestrator = build_orchestrator_from_env()
    state = await orchestrator.run(context)

    logger.info(
        "research_for_agent: wl=%d turns=%d offers=%d cost=$%.4f "
        "domains_visited=%d action_memory_urls=%d vision_fallbacks=%d",
        watchlist_id, state.turn, len(state.offers), state.cost_usd,
        state.sufficiency.distinct_domains_visited,
        len(state.action_memory),
        len(state.vision_fallback_log),
    )

    # 3. Persist offers as Deal rows (upsert on url).
    persisted = await _persist_offers(state.offers, context)

    return {
        "watchlist_id": watchlist_id,
        "turns_used": state.turn,
        "offer_count": len(state.offers),
        "persisted": persisted,
        "domains_visited": state.sufficiency.distinct_domains_visited,
        "vision_fallback_count": len(state.vision_fallback_log),
        "stop_reason": _stop_reason(state),
    }


def _stop_reason(state: OrchestratorState) -> str:
    if state.sufficiency.can_stop():
        return "sufficiency_met"
    if state.cost_usd > 0:
        return "budget_or_turns_exhausted"
    return "turns_exhausted"


async def _persist_offers(
    offers: list[DealOffer], context: WatchlistContext,
) -> int:
    """Upsert DealOffer → deals table. Returns count successfully written."""
    if not offers:
        return 0

    now = datetime.now(timezone.utc)
    written = 0
    async with get_async_session() as session:
        for offer in offers:
            listed = offer.listed_price if offer.listed_price else offer.price
            real_disc = None
            if listed and listed > offer.price:
                real_disc = round((listed - offer.price) / listed * 100.0, 1)

            stmt = (
                pg_insert(Deal)
                .values(
                    title=offer.title,
                    source=offer.retailer,
                    url=offer.url,
                    listed_price=listed,
                    sale_price=offer.price,
                    category=context.product_query[:128],
                    tags=json.dumps([]),
                    confidence="high",       # validator-approved
                    real_discount_pct=real_disc,
                    student_eligible=False,
                    condition=offer.condition,
                    legitimate=True,
                    hunt_date=date.today(),
                    first_seen_at=now,
                    scraped_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["url"],
                    set_={
                        "title": offer.title,
                        "sale_price": offer.price,
                        "listed_price": listed,
                        "real_discount_pct": real_disc,
                        "condition": offer.condition,
                        "scraped_at": now,
                    },
                )
            )
            try:
                await session.execute(stmt)
                written += 1
            except Exception:
                logger.exception(
                    "research_for_agent: persist failed for offer %r", offer.title,
                )
        await session.commit()
    return written


