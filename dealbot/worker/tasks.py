from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from dealbot.agents.composition import run_hunt
from dealbot.agents.workers.extractor import Offer
from dealbot.db.database import get_async_session
from dealbot.db.models import Deal, Watchlist
from dealbot.schemas import WatchlistContext
from dealbot.worker.celery_app import app

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# research_for_agent — v14. Drives the Explorer/ExtractorPool hunt pipeline
# end-to-end + persists Offers to the Deal table.
# ---------------------------------------------------------------------------

@app.task(name="dealbot.worker.tasks.research_for_agent", bind=True, max_retries=3)
def research_for_agent(self, watchlist_id: int) -> dict:
    """Run the v14 hunt pipeline for a single watchlist; persist offers."""
    try:
        return asyncio.run(_run_hunt_and_persist(watchlist_id))
    except Exception as exc:
        logger.exception("research_for_agent failed for wl=%d: %s", watchlist_id, exc)
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 60)


async def _run_hunt_and_persist(watchlist_id: int) -> dict:
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

    # 2. Run the v14 hunt pipeline. AGENT_BROWSER_BACKEND env var picks
    # Browserbase (production) or local Playwright (dev/eval).
    offers = await run_hunt(context)

    logger.info(
        "research_for_agent: wl=%d offers=%d",
        watchlist_id, len(offers),
    )

    # 3. Persist offers as Deal rows (upsert on url).
    persisted = await _persist_offers(offers, context)

    return {
        "watchlist_id": watchlist_id,
        "offer_count": len(offers),
        "persisted": persisted,
    }


async def _persist_offers(
    offers: list[Offer], context: WatchlistContext,
) -> int:
    """Upsert Offer → deals table. Returns count successfully written."""
    if not offers:
        return 0

    now = datetime.now(timezone.utc)
    written = 0
    async with get_async_session() as session:
        for offer in offers:
            # v14 Offer has no listed vs sale distinction — extractors read
            # the visible price from SERP cards. Populate both fields with
            # the observed price; real_discount_pct stays None.
            stmt = (
                pg_insert(Deal)
                .values(
                    title=offer.title,
                    source=offer.marketplace,
                    url=offer.url,
                    listed_price=offer.price,
                    sale_price=offer.price,
                    category=context.product_query[:128],
                    tags=json.dumps([]),
                    confidence="high",
                    real_discount_pct=None,
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
                        "listed_price": offer.price,
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
