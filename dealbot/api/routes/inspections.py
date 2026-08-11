"""Deal Inspector API: send a listing to Scout, read the report, chat about it.

POST /listings/{id}/inspect     run (or read cached) Tier A inspection
GET  /listings/{id}/inspection  cached report or 404
POST /listings/{id}/chat        stateless follow-up turn over the report

The chat is grounded, not agentic: Scout answers from what the inspection
already saw (report + extracted detail + playbook/profile when a watchlist
is given). Requests for new work (revisit, contact the seller) get an honest
text answer, never a browser session.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel

from datetime import datetime, timezone

from sqlalchemy import or_, select

from dealbot.agents.playbook import sanitize
from dealbot.api.auth import get_current_user
from dealbot.db.database import get_async_session
from dealbot.db.models import (
    InspectionChecklist,
    InspectionMessage,
    InspectionWatch,
    Listing,
    ListingInspection,
    User,
    Watchlist,
)
from dealbot.llm.base import LLMClient
from dealbot.schemas import ChatMessage, WatchlistContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/listings", tags=["inspections"])

_CHAT_HISTORY_MAX = 30
FREE_INSPECTIONS_PER_MONTH = 10


def _month_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


async def _check_allowance(user_id: int) -> bool:
    """True if this user may run a FRESH inspection this month. Lazy monthly
    reset; cached reads never reach this. Pro is uncapped."""
    async with get_async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return False
        if user.is_pro:
            return True
        month = _month_now()
        if user.inspections_month != month:
            user.inspections_month = month
            user.inspections_used = 0
            await session.commit()
        return user.inspections_used < FREE_INSPECTIONS_PER_MONTH


async def _count_inspection(user_id: int) -> None:
    async with get_async_session() as session:
        user = await session.get(User, user_id)
        if user is not None and not user.is_pro:
            user.inspections_used += 1
            await session.commit()


async def _watchlist_market(watchlist: Watchlist, context: WatchlistContext | None) -> dict | None:
    """Agent-scoped market numbers so listing chats and verdicts can place
    THIS deal against the playbook's rates. Best-effort."""
    try:
        from dealbot.recsys.market_stats import agent_comps, compute_market

        comps = await agent_comps(watchlist)
        return compute_market(comps, context)
    except Exception:
        logger.warning("market grounding failed for wl %d", watchlist.id, exc_info=True)
        return None


async def _record_watch(user_id: int, listing: Listing) -> None:
    """Inspecting = interest: start (or keep) a price-drop watch. Best-effort."""
    try:
        async with get_async_session() as session:
            existing = await session.get(InspectionWatch, (user_id, listing.id))
            if existing is None:
                session.add(InspectionWatch(
                    user_id=user_id, listing_id=listing.id,
                    price_at_inspection=listing.price,
                ))
                await session.commit()
    except Exception:
        logger.warning("inspection watch record failed (user %d, listing %d)",
                       user_id, listing.id, exc_info=True)


def _chat_llm() -> LLMClient:
    backend = os.environ.get("LLM_BACKEND", "openai")
    if backend == "bedrock":
        from dealbot.llm.bedrock_client import DEFAULT_NAV_MODEL, BedrockClient
        return BedrockClient(model=os.environ.get("BEDROCK_SCOUT_MODEL", DEFAULT_NAV_MODEL))
    from dealbot.llm.openai_client import OpenAIClient
    return OpenAIClient()


# Link-in (copilot spec 2026-08-10, phase A): a pasted URL resolves to a pool
# listing by the same canonical key ingest dedupes on, so share links and
# tracking-param variants all land. Grounding agent = closest intent match
# within the retrieval gate's similarity floor; the UI confirms before use.
_HOST_MARKETPLACES = (
    ("facebook.com", "fb_marketplace"),
    ("kijiji.ca", "kijiji"),
    ("ebay.", "ebay"),
    ("craigslist.org", "craigslist"),
)


def _marketplace_for_url(url: str) -> str | None:
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower()
    for needle, marketplace in _HOST_MARKETPLACES:
        if needle in host:
            return marketplace
    return None


class ResolveRequest(BaseModel):
    url: str


class ResolveListing(BaseModel):
    id: int
    title: str
    price: float
    currency: str
    marketplace: str
    url: str
    image_url: str | None


class ResolveWatchlist(BaseModel):
    id: int
    name: str


class ResolveResponse(BaseModel):
    listing: ResolveListing | None
    watchlist: ResolveWatchlist | None


@router.post("/resolve", response_model=ResolveResponse)
async def resolve_listing(
    body: ResolveRequest,
    current_user: User = Depends(get_current_user),
) -> ResolveResponse:
    from dealbot.persistence.canonicalize import canonicalize_url
    from dealbot.recsys.gate import GATE_SIMILARITY_TAU

    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Empty URL.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    marketplace = _marketplace_for_url(url)
    canonical = canonicalize_url(url, marketplace or "")

    async with get_async_session() as session:
        listing = (await session.execute(
            select(Listing)
            .where(or_(Listing.canonical_url == canonical, Listing.raw_url == url))
            .limit(1)
        )).scalars().first()

        watchlist = None
        if listing is not None and listing.embedding is not None:
            distance_cutoff = 1.0 - GATE_SIMILARITY_TAU
            watchlist = (await session.execute(
                select(Watchlist)
                .where(Watchlist.user_id == current_user.id)
                .where(Watchlist.intent_embedding.isnot(None))
                .where(Watchlist.intent_embedding.cosine_distance(listing.embedding) < distance_cutoff)
                .order_by(Watchlist.intent_embedding.cosine_distance(listing.embedding))
                .limit(1)
            )).scalars().first()

    return ResolveResponse(
        listing=ResolveListing(
            id=listing.id, title=listing.title, price=listing.price,
            currency=listing.currency, marketplace=listing.marketplace,
            url=listing.raw_url, image_url=listing.image_url,
        ) if listing is not None else None,
        watchlist=ResolveWatchlist(id=watchlist.id, name=watchlist.name)
        if watchlist is not None else None,
    )


class FetchRequest(BaseModel):
    url: str
    manual_price: float | None = None  # buyer-supplied when the page hides it


class FetchPartial(BaseModel):
    title: str
    image_url: str | None = None
    location: str | None = None


class FetchResponse(BaseModel):
    status: str                        # fetched | needs_price | failed | unsupported
    listing: ResolveListing | None = None
    watchlist: ResolveWatchlist | None = None
    partial: FetchPartial | None = None


@router.post("/fetch", response_model=FetchResponse)
async def fetch_listing_by_url(
    body: FetchRequest,
    current_user: User = Depends(get_current_user),
) -> FetchResponse:
    """Phase D: Scout goes and grabs a listing it has never seen. One browser
    visit + one cheap extraction call; gated on (but not counted against) the
    fresh-look allowance — the inspection that follows pays the vision bill."""
    from dealbot.agents.fetch_listing import FETCHABLE_MARKETPLACES, fetch_and_persist
    from dealbot.recsys.gate import GATE_SIMILARITY_TAU

    url = body.url.strip()
    if not url:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Empty URL.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    marketplace = _marketplace_for_url(url)
    if marketplace not in FETCHABLE_MARKETPLACES:
        return FetchResponse(status="unsupported")
    if not await _check_allowance(current_user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Fetching new listings shares your monthly fresh-look allowance, "
                   "and it is used up. Upgrade to Pro for unlimited looks.",
        )

    listing, partial = await fetch_and_persist(url, marketplace, body.manual_price)
    if listing is None and partial is not None:
        return FetchResponse(status="needs_price", partial=FetchPartial(**partial))
    if listing is None:
        return FetchResponse(status="failed")

    watchlist = None
    if listing.embedding is not None:
        async with get_async_session() as session:
            distance_cutoff = 1.0 - GATE_SIMILARITY_TAU
            watchlist = (await session.execute(
                select(Watchlist)
                .where(Watchlist.user_id == current_user.id)
                .where(Watchlist.intent_embedding.isnot(None))
                .where(Watchlist.intent_embedding.cosine_distance(listing.embedding) < distance_cutoff)
                .order_by(Watchlist.intent_embedding.cosine_distance(listing.embedding))
                .limit(1)
            )).scalars().first()

    return FetchResponse(
        status="fetched",
        listing=ResolveListing(
            id=listing.id, title=listing.title, price=listing.price,
            currency=listing.currency, marketplace=listing.marketplace,
            url=listing.raw_url, image_url=listing.image_url,
        ),
        watchlist=ResolveWatchlist(id=watchlist.id, name=watchlist.name)
        if watchlist is not None else None,
    )


class InspectionResponse(BaseModel):
    status: str                      # ok | listing_gone | error
    report: dict | None = None
    detail: dict | None = None
    comps: list[dict] = []
    cached: bool = False
    created_at: str | None = None


class InspectionChatRequest(BaseModel):
    messages: list[ChatMessage]
    watchlist_id: int | None = None
    image_keys: list[str] = []         # screenshots attached to the LAST user message
    tailoring_answer: bool = False     # the LAST user message answers Scout's question


class InspectionChatResponse(BaseModel):
    reply: str
    checklist: dict | None = None      # updated ready-to-buy state, when one exists
    send_next: str | None = None       # ready-to-send seller message, when the moment calls
    closer: str | None = None          # the finish-line call, fired once when ready flips


@router.post("/{listing_id}/inspect", response_model=InspectionResponse)
async def inspect_listing(
    listing_id: int,
    force: bool = Query(False),
    current_user: User = Depends(get_current_user),
) -> InspectionResponse:
    from dealbot.agents.inspector import get_cached_inspection, get_or_create_inspection

    async with get_async_session() as session:
        listing = await session.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Listing not found.")

    fresh_needed = force or (await get_cached_inspection(listing_id)) is None
    if fresh_needed and not await _check_allowance(current_user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Scout has used up this month's free looks. Pro removes the "
                "cap and inspects your top matches automatically."
            ),
        )

    result = await get_or_create_inspection(listing_id, force=force)
    if fresh_needed and result["status"] in ("ok", "listing_gone"):
        # A real look happened: count it against the allowance.
        await _count_inspection(current_user.id)
    if result["status"] == "ok":
        await _record_watch(current_user.id, listing)
    return InspectionResponse(**result)


@router.get("/{listing_id}/inspection", response_model=InspectionResponse)
async def read_inspection(
    listing_id: int,
    current_user: User = Depends(get_current_user),
) -> InspectionResponse:
    from dealbot.agents.inspector import get_cached_inspection

    cached = await get_cached_inspection(listing_id)
    if cached is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not inspected yet.")
    return InspectionResponse(**cached)


# ---------------------------------------------------------------------------
# Ready-to-buy checklist (copilot spec 2026-08-10, phase C)
# ---------------------------------------------------------------------------

import re as _re

_CHECKLIST_MAX = 5


def checklist_from_playbook(playbook: str | None) -> list[dict]:
    """Discrete checks from the playbook's 'What to check' section: each
    sentence is one thing a buyer verifies before paying."""
    if not playbook:
        return []
    start = playbook.find("What to check")
    if start == -1:
        return []
    rest = playbook[start + len("What to check"):]
    end = rest.find("The going rate")
    section = (rest[:end] if end != -1 else rest).strip()
    sentences = _re.findall(r"[^.!?\n]+[.!?]", section)
    checks = [t.strip() for t in sentences if len(t.strip()) > 12]
    return [{"check": c, "status": "open", "evidence": None} for c in checks[:_CHECKLIST_MAX]]


CRITERIA_SEED_SYSTEM = """You are Scout. Build the buyer's pre-purchase criteria
for ONE secondhand listing: the specific things that must be true for it to be
worth buying. You get generic category checks plus your own inspection notes
for THIS listing (what the photos showed, what you could not tell, red flags,
questions worth asking the seller).

Output JSON:
{"criteria": [{"check": str, "status": "open"|"satisfied"|"flagged",
               "evidence": str|null,
               "verify_via": "ask_seller"|"at_pickup"|"confirmed"}]}

- 3-5 criteria. Keep the category checks that matter for THIS listing, add
  listing-specific ones from the notes, dedupe overlaps.
- "satisfied" ONLY when the notes already settle it; then verify_via is
  "confirmed" and evidence cites the source ("photos show clean cups").
- "flagged" only when the notes show a real problem. Unverifiable is "open".
- verify_via for open items: "ask_seller" when a message can settle it,
  "at_pickup" when it needs hands-on.
- Each check under 90 chars, imperative, plain language. Evidence under 90
  chars.

Also decide ONE optional tailoring question, in the same JSON:
"tailoring": {"question": str, "chips": [str, ...]} | null
- Ask ONLY when the buyer's answer would genuinely change your call: the
  only knock is cosmetic and you don't know if they care about looks; the
  fit depends on what it's for. null is the common answer.
- Sounds like a friend, under 120 chars ("do you care how they look, or
  just how they sound?"). chips: 2-3 short answers a person would actually
  tap, in their voice ("looks matter", "just the sound").
- Never use em dashes. JSON only."""


CHECKLIST_ASSESS_SYSTEM = """You are Scout. A buyer has a pre-purchase checklist for
one secondhand listing. New evidence just arrived. Decide which OPEN checks the
evidence clearly settles, and whether it raises anything NEW that belongs on
the list.

Output JSON:
{"updates": [{"index": int, "status": "satisfied"|"flagged", "evidence": str}],
 "additions": [{"check": str, "status": "open"|"flagged", "evidence": str|null,
                "verify_via": "ask_seller"|"at_pickup"}]}

- updates: "satisfied" when the evidence positively shows a check passes for
  THIS listing; "flagged" when it shows the check FAILS (a visible defect, a
  seller admission, a confirmed problem).
- Evidence that is missing, unclear, or "cannot be verified from here" settles
  NOTHING: leave those checks open. Unverifiable is open, never flagged.
- additions: ONLY for a genuinely NEW concern that would change the buy
  decision (a seller admission like "one ear crackles", a defect visible in a
  screenshot, a dealbreaker the buyer just named). Not restatements of
  existing checks, not curiosities. Usually empty.
- "evidence" under 90 chars, plain language, naming the source. Checks under
  90 chars, imperative.
- An empty answer on both lists is normal, and the most common one.
- Never use em dashes. JSON only."""

_CHECKLIST_ADDS_MAX = 3
_VERIFY_VIA = ("ask_seller", "at_pickup", "confirmed")


def _criteria_from_payload(parsed: dict) -> list[dict]:
    """Validated criteria items from the seed call; [] when unusable."""
    items: list[dict] = []
    for entry in (parsed.get("criteria") or [])[:5]:
        if not isinstance(entry, dict):
            continue
        check = sanitize(str(entry.get("check") or "").strip())
        if len(check) < 8:
            continue
        status_val = entry.get("status")
        status_val = status_val if status_val in ("open", "satisfied", "flagged") else "open"
        verify = entry.get("verify_via")
        verify = verify if verify in _VERIFY_VIA else ("confirmed" if status_val == "satisfied" else None)
        evidence = entry.get("evidence")
        evidence = sanitize(str(evidence).strip())[:120] if isinstance(evidence, str) and evidence.strip() else None
        items.append({
            "check": check[:120], "status": status_val,
            "evidence": evidence, "verify_via": verify,
        })
    return items


def _apply_assessment(items: list[dict], parsed: dict) -> list[dict]:
    """Pure application of an assess payload: settle open checks, append
    bounded additions (marked `added`). Never mutates the input list."""
    out = [dict(item) for item in items]
    open_idx = {i for i, item in enumerate(out) if item.get("status") == "open"}
    for update in (parsed.get("updates") or []):
        if not isinstance(update, dict):
            continue
        index = update.get("index")
        status_val = update.get("status")
        if index in open_idx and status_val in ("satisfied", "flagged"):
            evidence = sanitize(str(update.get("evidence") or "").strip())[:120] or None
            out[index] = {**out[index], "status": status_val, "evidence": evidence}

    added_room = _CHECKLIST_ADDS_MAX - sum(1 for item in out if item.get("added"))
    for entry in (parsed.get("additions") or []):
        if added_room <= 0 or not isinstance(entry, dict):
            break
        check = sanitize(str(entry.get("check") or "").strip())
        if len(check) < 8:
            continue
        status_val = entry.get("status")
        verify = entry.get("verify_via")
        evidence = entry.get("evidence")
        out.append({
            "check": check[:120],
            "status": status_val if status_val in ("open", "flagged") else "open",
            "evidence": sanitize(str(evidence).strip())[:120] if isinstance(evidence, str) and evidence.strip() else None,
            "verify_via": verify if verify in ("ask_seller", "at_pickup") else None,
            "added": True,
        })
        added_room -= 1
    return out


def _tailoring_from_payload(parsed: dict) -> dict | None:
    """Validated {question, chips, answer:None} from the seed call; None when
    the model (correctly, usually) declined to ask."""
    entry = parsed.get("tailoring")
    if not isinstance(entry, dict):
        return None
    question = sanitize(str(entry.get("question") or "").strip())
    if len(question) < 10:
        return None
    chips = [
        sanitize(str(c).strip())[:40]
        for c in (entry.get("chips") or []) if isinstance(c, str) and c.strip()
    ][:3]
    return {"question": question[:160], "chips": chips, "answer": None}


async def _seed_criteria(
    playbook_checks: list[str], report: dict,
) -> tuple[list[dict] | None, dict | None]:
    """One LLM call merging category checks with THIS listing's unknowns,
    plus the optional verdict-flipping tailoring question.
    (None, None) on any failure — callers fall back to the deterministic seed."""
    payload = {
        "category_checks": playbook_checks,
        "inspection_notes": {
            k: report.get(k)
            for k in ("identification", "condition", "cant_tell", "red_flags", "seller_questions")
        },
    }
    try:
        response = await _chat_llm().complete(
            [
                {"role": "system", "content": CRITERIA_SEED_SYSTEM},
                {"role": "user", "content": json.dumps(payload)},
            ],
            response_format={"type": "json_object"},
        )
        parsed = json.loads(response.content or "{}")
        items = _criteria_from_payload(parsed)
        return (items or None), _tailoring_from_payload(parsed)
    except Exception:
        logger.warning("criteria seed failed", exc_info=True)
        return None, None


async def _assess_checklist(items: list[dict], evidence: str) -> list[dict]:
    """One cheap JSON call: settle open checks against new evidence and admit
    bounded new concerns. Returns updated items; the original on any failure."""
    open_items = [i for i, item in enumerate(items) if item.get("status") == "open"]
    if not evidence.strip():
        return items
    payload = {
        "checklist": [{"index": i, "check": items[i]["check"]} for i in open_items],
        "evidence": evidence[:4000],
    }
    try:
        response = await _chat_llm().complete(
            [
                {"role": "system", "content": CHECKLIST_ASSESS_SYSTEM},
                {"role": "user", "content": json.dumps(payload)},
            ],
            response_format={"type": "json_object"},
        )
        return _apply_assessment(items, json.loads(response.content or "{}"))
    except Exception:
        logger.warning("checklist assess failed", exc_info=True)
        return items


class ChecklistItem(BaseModel):
    check: str
    status: str                    # open | satisfied | flagged
    evidence: str | None = None
    verify_via: str | None = None  # ask_seller | at_pickup | confirmed
    added: bool = False            # admitted mid-conversation by the assessor


class ChecklistResponse(BaseModel):
    items: list[ChecklistItem]
    ready: bool
    watchlist_id: int | None = None
    tailoring: dict | None = None      # {question, chips, answer} when Scout asked


def _items_ready(items: list) -> bool:
    return (
        len(items) > 0
        and all(i.get("status") == "satisfied" for i in items)
    )


def _checklist_response(row: InspectionChecklist) -> ChecklistResponse:
    items = [ChecklistItem(**item) for item in row.items]
    # Ready means NOTHING in the way: every check satisfied, none flagged.
    ready = (
        len(items) > 0
        and all(i.status == "satisfied" for i in items)
        and not any(i.status == "flagged" for i in items)
    )
    tailoring = None
    if row.purchase_context:
        try:
            tailoring = json.loads(row.purchase_context)
        except (json.JSONDecodeError, TypeError):
            tailoring = None
    return ChecklistResponse(
        items=items, ready=ready, watchlist_id=row.watchlist_id, tailoring=tailoring,
    )


async def _get_or_seed_checklist(
    user_id: int, listing_id: int, watchlist_id: int | None,
) -> InspectionChecklist | None:
    """Existing row, or seed one and immediately assess it against the cached
    inspection report. With an agent: playbook checks merge with the report's
    unknowns. Without one (spec Pillar 1, no-agent inspections): the report's
    own fields ARE the criteria. None when there's nothing to seed from."""
    from dealbot.agents.inspector import get_cached_inspection

    async with get_async_session() as session:
        row = await session.get(InspectionChecklist, (user_id, listing_id))
        if row is not None:
            return row
        items: list[dict] = []
        if watchlist_id is not None:
            watchlist = await session.get(Watchlist, watchlist_id)
            if watchlist is None or watchlist.user_id != user_id:
                return None
            items = checklist_from_playbook(watchlist.playbook)

    tailoring = None
    inspection = await get_cached_inspection(listing_id)
    if inspection and inspection.get("report"):
        merged, tailoring = await _seed_criteria([i["check"] for i in items], inspection["report"])
        if merged is not None:
            items = merged
        elif items:
            # Seed call failed: settle the deterministic checks against the
            # report the old way rather than shipping an unassessed list.
            items = await _assess_checklist(
                items, "Scout's inspection notes: " + json.dumps(inspection["report"]),
            )
    if not items:
        return None

    async with get_async_session() as session:
        row = await session.get(InspectionChecklist, (user_id, listing_id))
        if row is None:
            row = InspectionChecklist(
                user_id=user_id, listing_id=listing_id,
                watchlist_id=watchlist_id, items=items,
                purchase_context=json.dumps(tailoring) if tailoring else None,
            )
            session.add(row)
            await session.commit()
        return row


@router.get("/{listing_id}/checklist", response_model=ChecklistResponse)
async def read_checklist(
    listing_id: int,
    watchlist_id: int | None = Query(None),
    current_user: User = Depends(get_current_user),
) -> ChecklistResponse:
    row = await _get_or_seed_checklist(current_user.id, listing_id, watchlist_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Nothing to build a checklist from yet.",
        )
    return _checklist_response(row)


class ChecklistToggle(BaseModel):
    index: int
    status: str                    # open | satisfied


@router.patch("/{listing_id}/checklist", response_model=ChecklistResponse)
async def toggle_checklist(
    listing_id: int,
    body: ChecklistToggle,
    current_user: User = Depends(get_current_user),
) -> ChecklistResponse:
    """Manual tick: the buyer knows things Scout can't see."""
    if body.status not in ("open", "satisfied"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Bad status.")
    async with get_async_session() as session:
        row = await session.get(InspectionChecklist, (current_user.id, listing_id))
        if row is None or not (0 <= body.index < len(row.items)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such check.")
        items = list(row.items)
        items[body.index] = {
            "check": items[body.index]["check"],
            "status": body.status,
            "evidence": "you checked this one off yourself" if body.status == "satisfied" else None,
        }
        row.items = items
        row.updated_at = datetime.now(timezone.utc)
        await session.commit()
        return _checklist_response(row)


VERDICT_SYSTEM = """You are Scout, the user's expert friend. You already inspected
this listing (your notes below). Give THE CALL for this specific buyer,
decision-first, in flowing prose (no headings, no lists):

- Open with the price call in one sentence, using ONLY the numbers provided
  ("$255 is fair for this market, typical is $279").
- If a ready-to-buy checklist is provided: every check satisfied means the
  next words are "Ready to buy." Anything open means "potentially a good buy
  IF", naming the open items in a few words. Anything flagged means being
  honest that it is not looking good, and why. No checklist: go straight to
  fit.
- Fit: one honest sentence on whether it suits what this buyer actually
  needs. Say so plainly if it does not.
- The number: what to offer and the walk-away, from the provided prices only.
- The opener: one short ready-to-send message to the seller making that
  offer, in quotes.

80-140 words. Second person, warm, direct. Plain text only: no markdown, no
asterisks, no bold, no headings. Never use em dashes. No greeting, no
sign-off."""


class VerdictRequest(BaseModel):
    watchlist_id: int


class VerdictResponse(BaseModel):
    verdict: str


@router.post("/{listing_id}/verdict", response_model=VerdictResponse)
async def inspection_verdict(
    listing_id: int,
    body: VerdictRequest,
    current_user: User = Depends(get_current_user),
) -> VerdictResponse:
    """Tier B: the personal read. Cheap text call over cached Tier A + the
    watchlist playbook + profile; computed on demand, never cached."""
    from dealbot.agents.inspector import get_cached_inspection

    async with get_async_session() as session:
        listing = await session.get(Listing, listing_id)
        watchlist = await session.get(Watchlist, body.watchlist_id)
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Listing not found.")
    if watchlist is None or watchlist.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found.")

    inspection = await get_cached_inspection(listing_id)
    if inspection is None or inspection["status"] != "ok":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Scout needs a successful look before giving a personal take.",
        )

    context = (
        WatchlistContext.model_validate_json(watchlist.context)
        if watchlist.context else None
    )
    market = await _watchlist_market(watchlist, context)
    async with get_async_session() as session:
        checklist_row = await session.get(
            InspectionChecklist, (current_user.id, listing_id)
        )
    purchase = None
    if checklist_row is not None and checklist_row.purchase_context:
        try:
            purchase = json.loads(checklist_row.purchase_context)
        except (json.JSONDecodeError, TypeError):
            purchase = None
    grounding = _grounding(listing, inspection, watchlist.playbook, context, market, purchase)
    if checklist_row is not None and checklist_row.items:
        grounding += "\n\nReady-to-buy checklist state: " + json.dumps(checklist_row.items)
    llm = _chat_llm()
    try:
        response = await llm.complete([
            {"role": "system", "content": VERDICT_SYSTEM},
            {"role": "user", "content": grounding},
        ])
        verdict = sanitize((response.content or "").strip())
    except Exception:
        logger.exception("verdict failed for listing %d", listing_id)
        verdict = ""
    if not verdict:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Scout could not put a take together just now. Try again shortly.",
        )
    return VerdictResponse(verdict=verdict)


CHAT_SYSTEM = """You are Scout, the user's expert friend who already took a close
look at ONE marketplace listing for them. The inspection notes below are what you
saw. The user is chatting with you about it, often WHILE talking to the seller
in another window. You are the other conversation: the wingman.

Output JSON: {"reply": str, "send_next": str|null}
- reply: your conversational answer. Ground every claim in the inspection
  notes, the listing facts, or the market numbers provided. If they ask
  something the inspection cannot answer, say so plainly and point at what
  would settle it.
- send_next: a short ready-to-send message TO THE SELLER, only when the moment
  calls for one: an unanswered ask-the-seller check being discussed, or a
  price move. Casual buyer's voice, under 200 chars. null on most turns.
- Negotiation: when the buyer reports a seller price, coach the counter using
  ONLY the market numbers provided (open, fair range, walk-away). Under the
  fair range is a deal worth taking; past the walk-away means walk. Never
  invent numbers.
- You cannot take actions yourself (no revisiting the page, no messaging the
  seller directly). Hand them exactly what to send instead.
- If buyer context is provided, tailor advice to it.
- Short, direct, warm. Plain language. Plain text inside the strings: no
  markdown, no asterisks, no bold. Never use em dashes. No stock closers.
  JSON only."""


def _grounding(listing: Listing, inspection: dict, playbook: str | None,
               context: WatchlistContext | None,
               market: dict | None = None,
               purchase: dict | None = None) -> str:
    parts = [
        f"Listing: {listing.title}",
        f"Price: ${listing.price:.2f} {listing.currency} on {listing.marketplace} · URL: {listing.raw_url}",
        f"Inspection status: {inspection['status']}",
    ]
    if inspection.get("report"):
        parts.append("Inspection notes (your own, from when you looked):")
        parts.append(json.dumps(inspection["report"], indent=1))
    if inspection.get("detail"):
        parts.append(f"Extracted page detail: {json.dumps(inspection['detail'])}")
    if context is not None:
        buyer = [f"Buyer's category: {context.product_query}"]
        if context.max_budget:
            buyer.append(f"budget ${context.max_budget:.0f}")
        if context.buyer_profile:
            buyer.append(f"profile: {context.buyer_profile}")
        parts.append("Buyer context: " + " · ".join(buyer))
    if market:
        parts.append(
            "Your market numbers for this category (computed; relate THIS "
            "listing's price to them when relevant): " + json.dumps({
                k: market.get(k) for k in ("typical", "band", "negotiation", "heat")
            })
        )
    if purchase and purchase.get("answer"):
        parts.append(
            "For THIS purchase the buyer told you "
            f"(answering your question \"{purchase.get('question', '')}\"): "
            f"{purchase['answer']}. Weigh the call accordingly."
        )
    if playbook:
        parts.append(f"Your playbook for this category:\n{playbook}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Screenshots in the chat (copilot spec 2026-08-10, phase B)
# ---------------------------------------------------------------------------

_IMAGES_PER_MESSAGE_MAX = 4


class ImageUploadResponse(BaseModel):
    key: str


@router.post("/{listing_id}/images", response_model=ImageUploadResponse)
async def upload_chat_image(
    listing_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
) -> ImageUploadResponse:
    """Stage one screenshot for this listing's chat. Storage only; the vision
    spend happens (and is metered) when the message carrying it is sent."""
    from dealbot.media import MAX_IMAGE_BYTES, save_image

    async with get_async_session() as session:
        listing = await session.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Listing not found.")

    data = await file.read(MAX_IMAGE_BYTES + 1)
    try:
        key = await save_image(data, file.content_type or "")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return ImageUploadResponse(key=key)


async def _image_parts(keys: list[str]) -> list[dict]:
    """Media keys → LLM image parts (data URLs). Missing keys are skipped:
    a lost screenshot degrades the message, never fails the chat."""
    import base64

    from dealbot.media import content_type_for, load_image

    parts: list[dict] = []
    for key in keys[:_IMAGES_PER_MESSAGE_MAX]:
        data = await load_image(key)
        if data is None:
            continue
        b64 = base64.b64encode(data).decode("ascii")
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{content_type_for(key)};base64,{b64}"},
        })
    return parts


@router.post("/{listing_id}/chat", response_model=InspectionChatResponse)
async def inspection_chat(
    listing_id: int,
    body: InspectionChatRequest,
    current_user: User = Depends(get_current_user),
) -> InspectionChatResponse:
    from dealbot.agents.inspector import get_cached_inspection

    async with get_async_session() as session:
        listing = await session.get(Listing, listing_id)
        watchlist = None
        if body.watchlist_id is not None:
            watchlist = await session.get(Watchlist, body.watchlist_id)
            if watchlist is None or watchlist.user_id != current_user.id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found.")
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Listing not found.")

    inspection = await get_cached_inspection(listing_id)
    if inspection is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Scout has not looked at this listing yet. Inspect it first.",
        )

    context = None
    playbook = None
    market = None
    if watchlist is not None:
        playbook = watchlist.playbook
        if watchlist.context:
            context = WatchlistContext.model_validate_json(watchlist.context)
        market = await _watchlist_market(watchlist, context)

    history = [
        {"role": m.role, "content": m.content}
        for m in body.messages[-_CHAT_HISTORY_MAX:]
        if m.role in ("user", "assistant")
    ]
    if not history:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Empty chat.")

    # Screenshots make this turn a vision call: it shares the fresh-look
    # monthly allowance (Pro uncapped). Storage was free; pixels are not.
    image_keys = [k for k in (body.image_keys or [])[:_IMAGES_PER_MESSAGE_MAX] if isinstance(k, str)]
    image_parts: list[dict] = []
    if image_keys and history[-1]["role"] == "user":
        if not await _check_allowance(current_user.id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Screenshot reads share your monthly fresh-look allowance, "
                       "and it is used up. Upgrade to Pro for unlimited looks.",
            )
        image_parts = await _image_parts(image_keys)

    # Purchase-level tailoring: a chip tap persists the answer before the
    # reply is generated, so the reply IS the re-tuned call.
    purchase = None
    async with get_async_session() as session:
        checklist_row = await session.get(
            InspectionChecklist, (current_user.id, listing_id)
        )
        if checklist_row is not None and checklist_row.purchase_context:
            try:
                purchase = json.loads(checklist_row.purchase_context)
            except (json.JSONDecodeError, TypeError):
                purchase = None
        if (
            body.tailoring_answer
            and purchase is not None
            and history[-1]["role"] == "user"
        ):
            purchase["answer"] = history[-1]["content"][:300]
            checklist_row.purchase_context = json.dumps(purchase)
            checklist_row.updated_at = datetime.now(timezone.utc)
            await session.commit()

    messages = [
        {"role": "system", "content": CHAT_SYSTEM},
        {"role": "system", "content": _grounding(listing, inspection, playbook, context, market, purchase)},
        *history,
    ]
    if image_parts:
        messages[-1] = {
            "role": "user",
            "content": [
                {"type": "text", "text": history[-1]["content"]
                    or "Here are screenshots I want you to look at."},
                *image_parts,
            ],
        }
    llm = _chat_llm()
    failed = False
    send_next = None
    try:
        response = await llm.complete(messages, response_format={"type": "json_object"})
        raw = (response.content or "").strip()
        try:
            parsed = json.loads(raw)
            reply = sanitize(str(parsed.get("reply", "")).strip()).replace("**", "")
            next_msg = parsed.get("send_next")
            if isinstance(next_msg, str) and next_msg.strip():
                send_next = sanitize(next_msg.strip())[:220]
        except (json.JSONDecodeError, TypeError):
            # Model ignored the schema: the raw text is still a usable reply.
            reply = sanitize(raw).replace("**", "")
    except Exception:
        logger.exception("inspection chat failed for listing %d", listing_id)
        reply = ""
    if not reply:
        failed = True
        reply = "I hit a snag answering that one. Give it another try in a moment."

    if not failed and image_parts:
        await _count_inspection(current_user.id)

    # A friend remembers the conversation: persist the exchange (but never a
    # snag apology — retries should not litter the thread). Best-effort.
    if not failed and history and history[-1]["role"] == "user":
        try:
            async with get_async_session() as session:
                session.add(InspectionMessage(
                    user_id=current_user.id, listing_id=listing_id,
                    role="user", content=history[-1]["content"],
                    images=image_keys or None,
                ))
                session.add(InspectionMessage(
                    user_id=current_user.id, listing_id=listing_id,
                    role="assistant", content=reply,
                ))
                await session.commit()
        except Exception:
            logger.warning("inspection message persist failed (listing %d)",
                           listing_id, exc_info=True)

    # The conversation is evidence: let it settle open checks. Best-effort.
    checklist_payload = None
    closer = None
    if not failed:
        try:
            async with get_async_session() as session:
                row = await session.get(
                    InspectionChecklist, (current_user.id, listing_id)
                )
            if row is not None and any(i.get("status") == "open" for i in row.items):
                was_ready = _items_ready(row.items)
                evidence = (
                    f"The buyer said: {history[-1]['content']}\n"
                    + (f"(The buyer attached {len(image_parts)} screenshot(s); "
                       f"Scout's reply below reflects what they show.)\n" if image_parts else "")
                    + f"Scout replied: {reply}"
                )
                items = await _assess_checklist(list(row.items), evidence)
                async with get_async_session() as session:
                    fresh = await session.get(
                        InspectionChecklist, (current_user.id, listing_id)
                    )
                    if fresh is not None:
                        fresh.items = items
                        fresh.updated_at = datetime.now(timezone.utc)
                        await session.commit()
                        checklist_payload = _checklist_response(fresh).model_dump()
                # The finish line: everything just checked out. Deterministic
                # numbers; posted once, persisted like any Scout message.
                if not was_ready and _items_ready(items):
                    negotiation = (market or {}).get("negotiation") if market else None
                    if negotiation:
                        closer = (
                            f"Ready to buy. Everything on the list checked out. "
                            f"Offer ${negotiation['open']}; anywhere under "
                            f"${negotiation['fair_high']} is a fair deal, and walk "
                            f"past ${negotiation['walk']}."
                        )
                    else:
                        closer = "Ready to buy. Everything on the list checked out."
                    try:
                        async with get_async_session() as session:
                            session.add(InspectionMessage(
                                user_id=current_user.id, listing_id=listing_id,
                                role="assistant", content=closer,
                            ))
                            await session.commit()
                    except Exception:
                        logger.warning("closer persist failed (listing %d)", listing_id)
        except Exception:
            logger.warning("chat checklist update failed (listing %d)",
                           listing_id, exc_info=True)
    return InspectionChatResponse(
        reply=reply, checklist=checklist_payload, send_next=send_next, closer=closer,
    )


# ---------------------------------------------------------------------------
# The pickup rundown (send-to-scout v2, stage 4)
# ---------------------------------------------------------------------------

MEETUP_SAFETY = ("Meet somewhere public and test before you pay. Cash or "
                 "e-transfer in person; never send a deposit to hold an item.")

RUNDOWN_SYSTEM = """You are Scout. The buyer is about to go pick up a secondhand
item. Turn their remaining checks into concrete test instructions, and their
flagged concerns into red lines.

Output JSON: {"at_pickup": [str], "walk_if": [str]}
- at_pickup: ONE imperative per open check, with the concrete HOW for this
  category ("play a bass-heavy track; listen for stutter in each ear").
  Under 90 chars each.
- walk_if: red lines from the flagged concerns only; nothing invented.
  Under 90 chars each.
- Never use em dashes. JSON only."""


class RundownRequest(BaseModel):
    watchlist_id: int


class RundownVerified(BaseModel):
    check: str
    evidence: str | None = None


class RundownResponse(BaseModel):
    deal: str
    verified: list[RundownVerified]
    at_pickup: list[str]
    walk_if: list[str]
    safety: str


def _rundown_text(r: "RundownResponse") -> str:
    lines = [f"PICKUP RUNDOWN · {r.deal}"]
    if r.verified:
        lines.append("Verified:")
        lines += [f"  ✓ {v.check}" + (f" ({v.evidence})" if v.evidence else "") for v in r.verified]
    if r.at_pickup:
        lines.append("Check at pickup:")
        lines += [f"  ! {t}" for t in r.at_pickup]
    if r.walk_if:
        lines.append("Walk if:")
        lines += [f"  ✕ {t}" for t in r.walk_if]
    lines.append(r.safety)
    return "\n".join(lines)


@router.post("/{listing_id}/rundown", response_model=RundownResponse)
async def pickup_rundown(
    listing_id: int,
    body: RundownRequest,
    current_user: User = Depends(get_current_user),
) -> RundownResponse:
    """The keepable parking-lot card. Checks and numbers assemble
    deterministically; the LLM only writes the how-to-test imperatives."""
    async with get_async_session() as session:
        listing = await session.get(Listing, listing_id)
        watchlist = await session.get(Watchlist, body.watchlist_id)
        row = await session.get(InspectionChecklist, (current_user.id, listing_id))
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Listing not found.")
    if watchlist is None or watchlist.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found.")
    if row is None or not row.items:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No checklist to run down yet.")

    context = (
        WatchlistContext.model_validate_json(watchlist.context)
        if watchlist.context else None
    )
    market = await _watchlist_market(watchlist, context)
    negotiation = (market or {}).get("negotiation") if market else None
    typical = (market or {}).get("typical") if market else None

    deal = f"{listing.title[:60]} · ${listing.price:.0f}"
    if typical:
        delta = round(typical - listing.price)
        if delta > 0:
            deal += f" · ${delta} under typical"
        elif delta < 0:
            deal += f" · ${-delta} over typical"

    verified = [
        RundownVerified(check=i["check"], evidence=i.get("evidence"))
        for i in row.items if i.get("status") == "satisfied"
    ]
    open_checks = [i["check"] for i in row.items if i.get("status") == "open"]
    flagged = [i["check"] for i in row.items if i.get("status") == "flagged"]

    at_pickup = list(open_checks)
    walk_if = list(flagged)
    if open_checks or flagged:
        try:
            response = await _chat_llm().complete(
                [
                    {"role": "system", "content": RUNDOWN_SYSTEM},
                    {"role": "user", "content": json.dumps({
                        "category": context.product_query if context else listing.title,
                        "open_checks": open_checks,
                        "flagged": flagged,
                    })},
                ],
                response_format={"type": "json_object"},
            )
            parsed = json.loads(response.content or "{}")
            polished = [sanitize(str(t).strip())[:110] for t in (parsed.get("at_pickup") or []) if isinstance(t, str) and t.strip()]
            if polished:
                at_pickup = polished[:6]
            red = [sanitize(str(t).strip())[:110] for t in (parsed.get("walk_if") or []) if isinstance(t, str) and t.strip()]
            if red or not flagged:
                walk_if = red[:4]
        except Exception:
            logger.warning("rundown polish failed for listing %d", listing_id, exc_info=True)

    if negotiation:
        walk_if.append(f"He moves the price past ${negotiation['walk']}")

    rundown = RundownResponse(
        deal=deal, verified=verified, at_pickup=at_pickup,
        walk_if=walk_if, safety=MEETUP_SAFETY,
    )

    # The thread keeps the card: reopening the listing lands on it.
    try:
        async with get_async_session() as session:
            session.add(InspectionMessage(
                user_id=current_user.id, listing_id=listing_id,
                role="assistant", content=_rundown_text(rundown),
            ))
            await session.commit()
    except Exception:
        logger.warning("rundown persist failed (listing %d)", listing_id)
    return rundown


@router.delete("/{listing_id}/thread", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(
    listing_id: int,
    current_user: User = Depends(get_current_user),
) -> None:
    """Remove this listing from the user's Scout page: their watch, their
    conversation, their checklist. The shared inspection cache stays — other
    users read it, and re-sending the listing later reuses it."""
    from sqlalchemy import delete as sql_delete

    async with get_async_session() as session:
        await session.execute(sql_delete(InspectionMessage).where(
            InspectionMessage.user_id == current_user.id,
            InspectionMessage.listing_id == listing_id,
        ))
        await session.execute(sql_delete(InspectionChecklist).where(
            InspectionChecklist.user_id == current_user.id,
            InspectionChecklist.listing_id == listing_id,
        ))
        await session.execute(sql_delete(InspectionWatch).where(
            InspectionWatch.user_id == current_user.id,
            InspectionWatch.listing_id == listing_id,
        ))
        await session.commit()


class ThreadMessage(BaseModel):
    role: str
    content: str
    created_at: str
    images: list[str] = []


class ThreadResponse(BaseModel):
    messages: list[ThreadMessage]


@router.get("/{listing_id}/messages", response_model=ThreadResponse)
async def read_thread(
    listing_id: int,
    current_user: User = Depends(get_current_user),
) -> ThreadResponse:
    async with get_async_session() as session:
        rows = (await session.execute(
            select(InspectionMessage)
            .where(InspectionMessage.user_id == current_user.id)
            .where(InspectionMessage.listing_id == listing_id)
            .order_by(InspectionMessage.id)
        )).scalars().all()
    return ThreadResponse(messages=[
        ThreadMessage(
            role=m.role, content=m.content,
            created_at=m.created_at.isoformat(),
            images=list(m.images) if m.images else [],
        )
        for m in rows
    ])


class InspectedItem(BaseModel):
    listing_id: int
    title: str
    price: float
    currency: str
    marketplace: str
    url: str
    image_url: str | None
    sold: bool
    price_dropped: bool
    price_at_inspection: float
    last_message: str | None
    inspected_at: str


class InspectedResponse(BaseModel):
    items: list[InspectedItem]


@router.get("/inspected", response_model=InspectedResponse)
async def inspected_listings(
    current_user: User = Depends(get_current_user),
) -> InspectedResponse:
    """Everything this user has sent to Scout, newest first: the revisit
    surface. Watches double as the send-to-Scout record."""
    async with get_async_session() as session:
        rows = (await session.execute(
            select(InspectionWatch, Listing)
            .join(Listing, Listing.id == InspectionWatch.listing_id)
            .where(InspectionWatch.user_id == current_user.id)
            .order_by(InspectionWatch.created_at.desc())
            .limit(100)
        )).all()

        items: list[InspectedItem] = []
        for watch, listing in rows:
            last = (await session.execute(
                select(InspectionMessage.content)
                .where(InspectionMessage.user_id == current_user.id)
                .where(InspectionMessage.listing_id == listing.id)
                .order_by(InspectionMessage.id.desc())
                .limit(1)
            )).scalar_one_or_none()
            items.append(InspectedItem(
                listing_id=listing.id,
                title=listing.title,
                price=listing.price,
                currency=listing.currency,
                marketplace=listing.marketplace,
                url=listing.raw_url,
                image_url=listing.image_url,
                sold=listing.sold_at is not None,
                price_dropped=listing.price < watch.price_at_inspection,
                price_at_inspection=watch.price_at_inspection,
                last_message=(last[:140] if last else None),
                inspected_at=watch.created_at.isoformat(),
            ))
    return InspectedResponse(items=items)
