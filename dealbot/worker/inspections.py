"""Post-hunt inspection work: price-drop watches and the Pro auto-inspect.

Both run fire-and-forget after every hunt. Both are best-effort: a failure
here never touches the hunt that spawned it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select

from dealbot.db.database import get_async_session
from dealbot.db.models import (
    Hunt,
    InspectionWatch,
    Listing,
    ListingAlert,
    ListingInspection,
    User,
    Watchlist,
    WatchlistRanking,
)
from dealbot.lifecycle import is_internal_user
from dealbot.notifications.email import build_price_drop_email, send_email, unsubscribe_url
from dealbot.schemas import WatchlistContext

logger = logging.getLogger(__name__)

# Top-5 pre-reads power the agent card's ✦ teasers (redesign spec 2026-08-09):
# system-initiated, cached per listing, never counted against the free
# allowance. Cost tracks NEW picks only (cache-by-listing absorbs repeats).
AUTO_INSPECT_TOP_N = 5

# Post-hunt inspections run a browser session each; 3 in parallel fits the
# headroom the session budget leaves beside hunt lanes.
AUTO_INSPECT_CONCURRENCY = int(os.environ.get("AUTO_INSPECT_CONCURRENCY", "3"))


async def _inspect_many(listing_ids: list[int]) -> list[dict | None]:
    """Bounded-parallel Tier A reads. Per-listing locks inside the inspector
    dedupe concurrent requests; failures return None and never propagate."""
    from dealbot.agents.inspector import get_or_create_inspection

    sem = asyncio.Semaphore(AUTO_INSPECT_CONCURRENCY)

    async def _one(listing_id: int) -> dict | None:
        async with sem:
            try:
                return await get_or_create_inspection(listing_id)
            except Exception:
                logger.exception("inspection failed for listing %d", listing_id)
                return None

    return list(await asyncio.gather(*(_one(l) for l in listing_ids)))


async def check_price_drops() -> int:
    """Email every watch whose listing now sits below its price-at-inspection.
    One notification per watch, ever (notified_at guards)."""
    notified = 0
    async with get_async_session() as session:
        rows = (await session.execute(
            select(InspectionWatch, Listing, User)
            .join(Listing, Listing.id == InspectionWatch.listing_id)
            .join(User, User.id == InspectionWatch.user_id)
            .where(InspectionWatch.notified_at.is_(None))
            .where(Listing.sold_at.is_(None))
            .where(Listing.price < InspectionWatch.price_at_inspection)
        )).all()

        for watch, listing, user in rows:
            if is_internal_user(user.email):
                watch.notified_at = datetime.now(timezone.utc)
                continue
            if not user.email_price_drops:
                continue        # watch stays armed in case they re-enable
            inspected_on = f"{watch.created_at:%b} {watch.created_at.day}" if watch.created_at else None
            subject, body, html_body = build_price_drop_email(
                listing, watch.price_at_inspection, inspected_on=inspected_on,
                unsub_url=unsubscribe_url(user.id, "price_drops"),
            )
            try:
                sent = await send_email(user.email, subject, body, html=html_body)
            except Exception:
                logger.exception("price-drop email failed (user %d)", user.id)
                continue
            if sent:
                watch.notified_at = datetime.now(timezone.utc)
                notified += 1
        await session.commit()
    return notified


# Quality-bar → acceptable condition grades (spec 2026-08-10). Grades outside
# the set demote a pick; "unknown" and missing flags always pass — never
# demote on ignorance.
_GRADES_ALLOWED = {
    "pristine": {"good"},
    "good": {"good", "fair"},
    "wear_ok": {"good", "fair", "worn"},
}
_TOP_PICKS = 5


def fails_quality_bar(bar: str | None, flags: dict | None) -> bool:
    """True when cached flags say this pick sits below the user's bar."""
    if bar not in _GRADES_ALLOWED or not flags:
        return False
    if flags.get("photos_real") is False:
        return True
    grade = flags.get("condition_grade")
    return grade in ("good", "fair", "worn") and grade not in _GRADES_ALLOWED[bar]


# The brief match (physical-brief spec 2026-08-10, stage 3): Scout's cached
# report text describes what the photos show; one cheap JSON call decides
# which top picks clearly contradict the buyer's brief. Unknown never demotes.
BRIEF_MATCH_SYSTEM = """A buyer described what their item should look like, and
may have hard requirements (handedness, set composition, included accessories).
For each listing you have Scout's inspection notes describing what its photos
show. Decide per listing whether it matches the buyer's brief.

Output JSON: {"matches": [{"id": int, "match": true|false|null}]}
- false ONLY when the notes clearly contradict the brief: wrong color, wrong
  shape or variant, a violated hard requirement, wear far beyond what the
  brief tolerates.
- true when the notes clearly fit the brief.
- null when the notes do not say enough to tell. Unknown is null, never false.
- JSON only."""


def _brief_llm():
    backend = os.environ.get("LLM_BACKEND", "openai")
    if backend == "bedrock":
        from dealbot.llm.bedrock_client import DEFAULT_NAV_MODEL, BedrockClient
        return BedrockClient(model=os.environ.get("BEDROCK_SCOUT_MODEL", DEFAULT_NAV_MODEL))
    from dealbot.llm.openai_client import OpenAIClient
    return OpenAIClient()


def _report_desc(report_json: str | None) -> str | None:
    """The report's physical description of a listing; None without one."""
    if not report_json:
        return None
    try:
        report = json.loads(report_json)
    except (json.JSONDecodeError, TypeError):
        return None
    parts = [report.get(k) for k in ("identification", "condition", "summary")]
    text = " ".join(p for p in parts if isinstance(p, str) and p)
    return text or None


async def _brief_match(brief: str, items: list[tuple[int, str]]) -> dict[int, bool | None]:
    """listing_id → true/false/None against the brief. {} on any failure —
    an unreachable model must never demote anything."""
    if not items:
        return {}
    payload = {
        "brief": brief,
        "listings": [{"id": listing_id, "notes": notes[:600]} for listing_id, notes in items],
    }
    known = {listing_id for listing_id, _ in items}
    try:
        response = await _brief_llm().complete(
            [
                {"role": "system", "content": BRIEF_MATCH_SYSTEM},
                {"role": "user", "content": json.dumps(payload)},
            ],
            response_format={"type": "json_object"},
        )
        parsed = json.loads(response.content or "{}")
        out: dict[int, bool | None] = {}
        for entry in parsed.get("matches") or []:
            listing_id = entry.get("id")
            verdict = entry.get("match")
            if listing_id in known and (verdict is True or verdict is False or verdict is None):
                out[listing_id] = verdict
        return out
    except Exception:
        logger.warning("brief match failed", exc_info=True)
        return {}


async def enforce_quality_bar(watchlist_id: int) -> int:
    """One bounded swap round over the curated top picks (spec 2026-08-10):
    demote picks whose flags fail the owner's quality bar OR whose inspection
    notes contradict their physical brief, then inspect whatever newly
    entered the top 5 (≤5 calls). Replacements are never re-demoted this
    round. Returns demoted count."""
    from dealbot.recsys.market_stats import WEAK_SCORE

    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
        if watchlist is None or not watchlist.context:
            return 0
        context = WatchlistContext.model_validate_json(watchlist.context)
    bar_active = context.quality_bar in _GRADES_ALLOWED
    brief = (context.appearance_notes or "").strip()
    # Must-tier attributes are hard requirements — they join the brief so a
    # left-handed set demotes even when no appearance notes were captured.
    musts = [a.value.strip() for a in context.attributes
             if a.tier == "must" and a.value.strip()]
    if musts:
        requirement = f"Hard requirements: {'; '.join(musts)}."
        brief = f"{brief} {requirement}".strip() if brief else requirement
    if not bar_active and not brief:
        return 0

    async with get_async_session() as session:
        curated = list((await session.execute(
            select(WatchlistRanking)
            .where(WatchlistRanking.watchlist_id == watchlist_id)
            .where(WatchlistRanking.score >= WEAK_SCORE)
            .order_by(WatchlistRanking.position)
        )).scalars().all())
        if len(curated) <= 1:
            return 0
        top_ids = [r.listing_id for r in curated[:_TOP_PICKS]]
        insp_rows = (await session.execute(
            select(ListingInspection)
            .where(ListingInspection.listing_id.in_(top_ids))
        )).scalars().all()
        flags_by_id = {row.listing_id: row.flags for row in insp_rows}
        desc_by_id = {row.listing_id: _report_desc(row.report) for row in insp_rows}

    failing_ids: set[int] = set()
    if bar_active:
        failing_ids |= {
            listing_id for listing_id in top_ids
            if fails_quality_bar(context.quality_bar, flags_by_id.get(listing_id))
        }
    if brief:
        candidates = [
            (listing_id, desc_by_id[listing_id])
            for listing_id in top_ids
            if listing_id not in failing_ids and desc_by_id.get(listing_id)
        ]
        verdicts = await _brief_match(brief, candidates)
        failing_ids |= {listing_id for listing_id, v in verdicts.items() if v is False}
    if not failing_ids:
        return 0

    # The LLM call ran outside any session; re-read the rows to reorder so a
    # concurrent recompute can't be half-overwritten with stale positions.
    async with get_async_session() as session:
        rows = list((await session.execute(
            select(WatchlistRanking)
            .where(WatchlistRanking.watchlist_id == watchlist_id)
            .where(WatchlistRanking.score >= WEAK_SCORE)
            .order_by(WatchlistRanking.position)
        )).scalars().all())
        keep = [r for r in rows if r.listing_id not in failing_ids]
        failing = [r for r in rows if r.listing_id in failing_ids]
        if not failing:
            return 0
        positions = sorted(r.position for r in rows)
        for pos, row in zip(positions, keep + failing):
            row.position = pos
        promoted = [r.listing_id for r in keep[:_TOP_PICKS]]
        await session.commit()

    # One round only: every uninspected pick in the new top 5 gets a read
    # (≤5 calls; keepers are usually cache hits), and nothing gets demoted
    # a second time this pass.
    async with get_async_session() as session:
        already = set((await session.execute(
            select(ListingInspection.listing_id)
            .where(ListingInspection.listing_id.in_(promoted))
        )).scalars().all())
    await _inspect_many([l for l in promoted if l not in already])

    logger.info("enforce_quality_bar: wl=%d demoted %d", watchlist_id, len(failing))
    return len(failing)


async def auto_inspect_top_matches(hunt_id: int, top_n: int = AUTO_INSPECT_TOP_N) -> int:
    """Pre-run Tier A on the hunt's best new matches so every agent card's
    top picks open with Scout's read already cached (all users; the card's
    teasers depend on it)."""
    async with get_async_session() as session:
        hunt = await session.get(Hunt, hunt_id)
        if hunt is None:
            return 0
        watchlist = await session.get(Watchlist, hunt.watchlist_id)
        if watchlist is None:
            return 0
        owner = await session.get(User, watchlist.user_id)
        if owner is None:
            return 0

        candidates = (await session.execute(
            select(ListingAlert.listing_id)
            .outerjoin(ListingInspection,
                       ListingInspection.listing_id == ListingAlert.listing_id)
            .where(ListingAlert.hunt_id == hunt_id)
            .where(ListingInspection.listing_id.is_(None))  # not yet inspected
            .order_by(ListingAlert.score.desc())
            .limit(top_n)
        )).scalars().all()

    results = await _inspect_many(list(candidates))
    inspected = sum(
        1 for r in results if r is not None and r["status"] in ("ok", "listing_gone")
    )

    try:
        await enforce_quality_bar(watchlist.id)
    except Exception:
        logger.exception("quality-bar pass failed for wl %d", watchlist.id)
    return inspected
