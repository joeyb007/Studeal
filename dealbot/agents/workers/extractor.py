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

import json
import logging
from typing import Literal
from urllib.parse import urljoin

from pydantic import BaseModel, ValidationError

from dealbot.agents.perception import PageSnapshot
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

        - Fresh message list per call (no accumulation across invocations).
        - marketplace is stamped onto every returned Offer (caller's context).
        - Malformed LLM output → returns [] (no partial data, no crashes).
        """
        messages = [
            {"role": "system", "content": EXTRACTOR_SYSTEM},
            {"role": "user", "content": _render_user_prompt(snap, marketplace, spec)},
        ]

        try:
            response = await self.llm.complete(
                messages, response_format={"type": "json_object"},
            )
        except Exception as exc:
            logger.warning("extractor: LLM call failed: %s", exc)
            return []

        return _parse_and_filter(response.content, marketplace, snap.url)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _render_user_prompt(
    snap: PageSnapshot, marketplace: str, spec: WatchlistContext,
) -> str:
    text = snap.text if len(snap.text) <= 18000 else snap.text[:18000] + "\n[...truncated]"
    return (
        f"Marketplace: {marketplace}\n"
        f"URL: {snap.url}\n"
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
        row = {**row, "marketplace": marketplace}
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
