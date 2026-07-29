from __future__ import annotations

import asyncio
import logging

from dealbot.agents.composition import run_hunt
from dealbot.db.database import get_async_session
from dealbot.db.models import Watchlist
from dealbot.persistence.listings import persist_offers
from dealbot.schemas import WatchlistContext
from dealbot.worker.celery_app import app

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# research_for_agent — v14. Drives the Explorer/ExtractorPool hunt pipeline
# end-to-end + persists Offers to the listings table (canonical-URL dedup).
# ---------------------------------------------------------------------------

@app.task(name="dealbot.worker.tasks.research_for_agent", bind=True, max_retries=3)
def research_for_agent(self, watchlist_id: int) -> dict:
    """Run the v14 hunt pipeline for a single watchlist; persist listings."""
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

    # 3. Persist offers as Listings (dedup by canonical URL).
    persisted = (await persist_offers(offers)).written

    logger.info(
        "research_for_agent: wl=%d offers=%d persisted=%d",
        watchlist_id, len(offers), persisted,
    )

    return {
        "watchlist_id": watchlist_id,
        "offer_count": len(offers),
        "persisted": persisted,
    }
