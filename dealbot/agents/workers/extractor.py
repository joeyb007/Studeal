"""Extractor — stateless snapshot-to-Offers worker.

Design (v14 pivot):

    Given a captured PageSnapshot, a single LLM call enumerates every listing
    card visible and returns them as structured Offers. No browser interaction
    (Explorer already handled scrolling / navigation), no tool loop, no
    accumulated state — pure function of (snapshot, marketplace, spec).

    Fed by the ExtractorPool consuming an asyncio.Queue that Explorers write
    to on every URL they visit. Extractor workers run in parallel across the
    pool.

Rationale: Explorer captures full-page state as it browses. Extraction becomes
a downstream concern with no page dependency — cheap to parallelize, fresh
context per invocation, trivial to test.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Literal
from urllib.parse import urljoin

from pydantic import BaseModel, ValidationError

from dealbot.agents.image_capture import attach_images
from dealbot.agents.perception import PageSnapshot, chunk_snapshot_text
from dealbot.persistence.canonicalize import canonicalize_url
from dealbot.llm.base import LLMClient
from dealbot.schemas import WatchlistContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class Offer(BaseModel):
    """Extracted listing card. All fields grounded in DOM observation."""

    title: str
    price: float
    currency: str = "USD"
    url: str                          # canonical listing URL
    image_url: str | None = None
    location: str | None = None
    posted_at_raw: str | None = None  # e.g. "2 days ago" — normalized later
    condition: Literal["new", "refurbished", "used", "unknown"] = "unknown"
    marketplace: str                  # supplied by caller (Explorer knew the site)


# ---------------------------------------------------------------------------
# Prompt (inlined; move to prompts.py in Pass 3 cleanup)
# ---------------------------------------------------------------------------

EXTRACTOR_SYSTEM = """You are a marketplace listing extractor. You receive a
captured page snapshot and a watchlist spec. Your job is to enumerate every
listing card visible in the snapshot and return them as structured JSON.

Return a single JSON object of the form:
  {"offers": [<Offer>, ...]}

Each Offer must contain: title, price, currency, url. Optional but preferred:
image_url, location, posted_at_raw, condition ("new"|"refurbished"|"used"|"unknown").

Rules:
  - Only emit offers grounded in the snapshot. Do not invent fields.
  - Skip cards missing a real URL or a positive price.
  - price is what the buyer PAYS right now. Retail pages surround it with
    decoys: "SAVE $200", struck-through was-prices, "$50 off", "$28/month"
    financing, add-on accessory prices. NEVER emit a savings amount, a
    was-price, or a per-month figure as the price. When a card shows both a
    sale price and a save amount, the price is the current total, not the
    discount. If you can only see a discount and no actual price, skip the
    card.
  - Do not filter by relevance to the spec — that's someone else's job. Emit
    every listing card you can see."""


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

class Extractor:
    """Single-shot snapshot-to-Offers worker. Fresh LLM context per call."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def extract_from_snapshot(
        self,
        snap: PageSnapshot,
        marketplace: str,
        spec: WatchlistContext,
    ) -> list[Offer]:
        """Emit every listing card the LLM identifies in the snapshot.

        Large pages are split into overlapping, recall-sized chunks and
        extracted CONCURRENTLY — a single window showed the model ~18k of a
        page that can run to 137k, so most listings were never seen. Overlap
        duplicates are collapsed here on canonical-ish url so callers get
        clean output.

        - Fresh message list per chunk (no accumulation across invocations).
        - marketplace is stamped onto every returned Offer (caller's context).
        - Malformed LLM output → that chunk yields nothing; others still land.
        """
        chunks = chunk_snapshot_text(_clip_hrefs(snap.text))
        results = await asyncio.gather(
            *(self._extract_chunk(chunk, snap, marketplace, spec) for chunk in chunks),
            return_exceptions=True,
        )

        offers: list[Offer] = []
        seen: set[str] = set()
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("extractor: chunk failed: %s", result)
                continue
            for offer in result:
                if offer.url in seen:
                    continue
                seen.add(offer.url)
                offers.append(offer)
        if len(chunks) > 1:
            logger.info(
                "extractor: %d chunks over %d chars → %d unique offers",
                len(chunks), len(snap.text), len(offers),
            )
        offers = _ground_offers(offers, snap, marketplace)
        # Thumbnails come ONLY from the deterministic capture map; any
        # image_url the LLM emitted is overwritten (join miss → None).
        attach_images(offers, snap.image_map, marketplace)
        return offers

    async def _extract_chunk(
        self,
        chunk_text: str,
        snap: PageSnapshot,
        marketplace: str,
        spec: WatchlistContext,
    ) -> list[Offer]:
        messages = [
            {"role": "system", "content": EXTRACTOR_SYSTEM},
            {"role": "user", "content": _render_user_prompt(
                chunk_text, snap.url, marketplace, spec,
            )},
        ]
        try:
            response = await self.llm.complete(
                messages, response_format={"type": "json_object"},
            )
        except Exception as exc:
            logger.warning("extractor: LLM call failed: %s", exc)
            return []
        parsed = _parse_and_filter(response.content, marketplace, snap.url)
        kept = [o for o in parsed if not _is_discount_decoy(chunk_text, o.price)]
        if len(kept) < len(parsed):
            logger.info(
                "extractor: dropped %d offer(s) whose price matches a "
                "SAVE/off amount on the page", len(parsed) - len(kept),
            )
        return kept


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_discount_decoy(page_text: str, price: float) -> bool:
    """True when the page explicitly labels this exact amount as a discount
    ("SAVE $200", "$200 off") — the model grabbed the decoy, not the price
    (observed 2026-08-13: a $599.99 open-box listing extracted as $200 from
    Best Buy's "SAVE $200" badge). Deterministic backstop behind the prompt
    rule; whole-dollar amounts only, with optional .00 and thousands commas.
    """
    if price != int(price):
        return False        # decoy badges are whole-dollar amounts
    whole = int(price)
    amount = rf"\$\s*(?:{whole}|{whole:,})(?:\.00)?"
    pattern = rf"(?:save\s*{amount}|{amount}\s*off\b)"
    return re.search(pattern, page_text, re.IGNORECASE) is not None


_HREF_CLIP_RE = re.compile(r'href="([^"]{200})[^"]*"')


def _clip_hrefs(text: str) -> str:
    """Marketplace tracking URLs run to ~800 chars of base64 noise each; at
    SERP density they swamp the extraction prompt (observed: gpt-4o-mini
    returning zero offers on a page it enumerates fine once clipped).

    Threshold sits above real listing paths (~150 chars on kijiji): clipping
    them mid-slug made the LLM invent URL completions with fabricated ad IDs
    (observed 2026-08-03), which are dead links and unjoinable to images."""
    return _HREF_CLIP_RE.sub(lambda m: f'href="{m.group(1)}…"', text)


def _ground_offers(
    offers: list[Offer],
    snap: PageSnapshot,
    marketplace: str,
) -> list[Offer]:
    """Drop offers whose URL matches no anchor actually present in the
    snapshot. An LLM completing a clipped href invents plausible listing
    URLs; a listing that can't be grounded to the page is a dead link and
    worthless downstream, so refusing it beats persisting it.

    Skipped when the snapshot exposes no hrefs at all (unit fixtures,
    degenerate pages): no evidence either way is not grounds to drop
    everything.
    """
    grounded: set[str] = set(snap.image_map)  # keys already canonical
    for ref in snap.element_map.values():
        href = ref.attributes.get("href")
        if href:
            try:
                grounded.add(canonicalize_url(urljoin(snap.url, href), marketplace))
            except Exception:
                continue
    if not grounded:
        return offers

    kept: list[Offer] = []
    dropped = 0
    for offer in offers:
        try:
            key = canonicalize_url(offer.url, marketplace)
        except Exception:
            dropped += 1
            continue
        if key in grounded:
            kept.append(offer)
        else:
            dropped += 1
    if dropped:
        logger.info(
            "extractor: dropped %d/%d ungrounded offers on %s (fabricated URLs)",
            dropped, len(offers), marketplace,
        )
    return kept


_PRICE_NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")
_CURRENCY_HINTS: tuple[tuple[str, str], ...] = (
    ("c $", "CAD"), ("ca$", "CAD"), ("cad", "CAD"),
    ("us $", "USD"), ("£", "GBP"), ("€", "EUR"),
)
# Ordered: first substring match wins ("like new" must beat "new").
_CONDITION_SYNONYMS: tuple[tuple[str, str], ...] = (
    ("refurb", "refurbished"), ("renewed", "refurbished"),
    ("like new", "used"), ("pre-owned", "used"), ("preowned", "used"),
    ("open box", "used"), ("open-box", "used"), ("used", "used"),
    ("new", "new"),
)
_VALID_CONDITIONS = {"new", "refurbished", "used", "unknown"}


def _normalize_row(row: dict) -> dict:
    """Tolerate the ways real pages phrase things — models echo the page.
    'C $153.99' prices and 'Pre-Owned' conditions killed whole SERPs via
    silent ValidationError drops before this."""
    price = row.get("price")
    if isinstance(price, str):
        lowered = price.lower()
        for hint, code in _CURRENCY_HINTS:
            if hint in lowered:
                row["currency"] = code
                break
        match = _PRICE_NUM_RE.search(price)
        if match:
            row["price"] = float(match.group(1).replace(",", ""))
    condition = row.get("condition")
    if isinstance(condition, str):
        c = condition.strip().lower()
        if c not in _VALID_CONDITIONS:
            for needle, mapped in _CONDITION_SYNONYMS:
                if needle in c:
                    row["condition"] = mapped
                    break
            else:
                row["condition"] = "unknown"
        else:
            row["condition"] = c
    return row


def _render_user_prompt(
    text: str, url: str, marketplace: str, spec: WatchlistContext,
) -> str:
    return (
        f"Marketplace: {marketplace}\n"
        f"URL: {url}\n"
        f"Watchlist spec: {spec.model_dump_json(indent=2)}\n\n"
        f"Page snapshot:\n{text}\n\n"
        "Emit the JSON object."
    )


def _parse_and_filter(content: str, marketplace: str, page_url: str) -> list[Offer]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    raw_offers = data.get("offers", [])
    if not isinstance(raw_offers, list):
        return []

    result: list[Offer] = []
    for row in raw_offers:
        if not isinstance(row, dict):
            continue
        # Stamp marketplace from caller's context; ignore any value the LLM
        # might have hallucinated in the row itself.
        row = _normalize_row({**row, "marketplace": marketplace})
        try:
            offer = Offer.model_validate(row)
        except ValidationError:
            continue
        if offer.price <= 0:
            continue
        if not offer.url:
            continue
        offer.url = urljoin(page_url, offer.url)
        if not offer.url.startswith(("http://", "https://")):
            logger.debug("extractor: dropping offer with unusable url %r", offer.url)
            continue
        result.append(offer)
    return result
