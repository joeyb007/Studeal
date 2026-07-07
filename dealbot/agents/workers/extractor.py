"""Extractor — page-locked LLM sub-worker with fresh context per invocation.

Design (from `docs/v14-architecture.md`, primitive P4):

    Given a live Playwright Page, uses a bounded ReAct loop (max 5 tool calls)
    to enumerate every listing card visible on the current URL. Fresh LLM
    message context per invocation — no history leaks across calls.

Tools available inside the loop:
    - scroll (max 3 uses): reveal below-fold cards
    - read_page (max 2 uses): re-snapshot after DOM mutation
    - emit_offers (terminal): LLM's final structured output; ends loop

Non-goals:
    - click, navigate — extractor is page-locked. Detail enrichment on a
      candidate URL is a separate sub-worker dispatched from a different page.
    - accumulating state across invocations. Each `extract_page` call starts
      with an empty message list.

Rationale: solves the OfferExtractor context-rot bug in v13. The DeepAgents
"subagent context isolation" pattern applied to marketplace SERP triage.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from playwright.async_api import Page
from pydantic import BaseModel, ValidationError

from dealbot.agents.perception import snapshot_page
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
    marketplace: str                  # inferred from URL host


# ---------------------------------------------------------------------------
# Prompt (inlined; move to prompts.py in Day 2 cleanup)
# ---------------------------------------------------------------------------

EXTRACTOR_SYSTEM = """You are a marketplace listing extractor. You are given a
single web page representing search results on a secondhand marketplace, and
a user's watchlist spec. Your job is to enumerate every listing card visible
and return them as structured JSON.

You are PAGE-LOCKED: you cannot click, cannot navigate, cannot open new tabs.
Your only actions are: scroll down (reveals lazy-loaded / below-fold cards),
read_page (re-snapshot the current page after scrolling), or emit_offers
(terminal — return your final structured output).

Each turn, emit ONE action as a JSON object. Valid actions:
  {"action":"scroll"}
  {"action":"read_page"}
  {"action":"emit_offers","offers":[<Offer>, ...]}

Each Offer object must contain: title, price, currency, url, marketplace.
Optional: image_url, location, posted_at_raw, condition ("new"|"refurbished"|"used"|"unknown").

Strategy:
  1. Read the page. If the visible listings look complete (no obvious pagination
     mid-scroll, no "showing X of Y" indicators higher than what you see),
     emit_offers directly.
  2. If there are likely more listings below the fold, scroll and re-read.
  3. When you have every listing card, emit_offers with all of them.
  4. Do NOT invent offers. Every field must be grounded in what the DOM shows.

You have a hard budget of 5 total tool calls per page. Use it wisely."""


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

class Extractor:
    """Page-locked LLM sub-worker with bounded ReAct loop."""

    MAX_TOOL_CALLS: int = 5
    MAX_SCROLLS: int = 3
    MAX_READ_PAGES: int = 2

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def extract_page(
        self, page: Page, spec: WatchlistContext,
    ) -> list[Offer]:
        """Bounded ReAct on the current page. Fresh context. Page-locked.

        See module docstring for full contract.
        """
        # Fresh message list per invocation — no history leaks across calls.
        snap = await snapshot_page(page)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": EXTRACTOR_SYSTEM},
            {"role": "user", "content": _render_initial_prompt(spec, snap)},
        ]

        scroll_used = 0
        read_page_used = 0

        for _ in range(self.MAX_TOOL_CALLS):
            response = await self.llm.complete(
                messages, response_format={"type": "json_object"},
            )
            messages.append({"role": "assistant", "content": response.content})

            action, parse_error = _parse_action(response.content)

            if parse_error is not None:
                messages.append({"role": "user", "content": (
                    f"Your action JSON was invalid: {parse_error}. "
                    "Re-emit one valid action."
                )})
                continue

            action_type = action.get("action")

            if action_type == "emit_offers":
                raw_offers = action.get("offers", [])
                return _parse_and_filter_offers(raw_offers)

            if action_type == "scroll":
                if scroll_used >= self.MAX_SCROLLS:
                    messages.append({"role": "user", "content": (
                        f"Scroll budget exhausted ({self.MAX_SCROLLS}/{self.MAX_SCROLLS}). "
                        "Emit read_page or emit_offers."
                    )})
                    continue
                try:
                    await page.evaluate(
                        "() => window.scrollBy(0, window.innerHeight)"
                    )
                except Exception as exc:
                    logger.debug("extractor: scroll failed: %s", exc)
                scroll_used += 1
                messages.append({"role": "user", "content": (
                    f"Scrolled ({scroll_used}/{self.MAX_SCROLLS}). "
                    "Emit next action."
                )})
                continue

            if action_type == "read_page":
                if read_page_used >= self.MAX_READ_PAGES:
                    messages.append({"role": "user", "content": (
                        f"read_page budget exhausted ({self.MAX_READ_PAGES}/"
                        f"{self.MAX_READ_PAGES}). Emit emit_offers now."
                    )})
                    continue
                snap = await snapshot_page(page)
                read_page_used += 1
                messages.append({"role": "user", "content": (
                    f"Re-snapshot ({read_page_used}/{self.MAX_READ_PAGES}). "
                    f"Updated page:\n{_page_block(snap)}\n"
                    "Emit next action."
                )})
                continue

            # Anything else — click, navigate, unknown — is a page-locked
            # violation. Feed back an error and keep looping.
            messages.append({"role": "user", "content": (
                f"Action {action_type!r} is not permitted. Extractor is "
                "page-locked. Valid actions: scroll, read_page, emit_offers."
            )})

        # Budget exhausted without emit_offers. Return empty (LLM never
        # committed to any offers we can trust).
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _render_initial_prompt(spec: WatchlistContext, snap: Any) -> str:
    spec_str = spec.model_dump_json(indent=2)
    return (
        f"Watchlist spec:\n{spec_str}\n\n"
        f"Current page:\n{_page_block(snap)}\n\n"
        "Emit the first action as JSON."
    )


def _page_block(snap: Any) -> str:
    """Render a page snapshot as a compact text block for the LLM.

    Caps at 18k chars — anything past that on marketplace SERPs is footer /
    related-products noise that adds latency without helping extraction.
    """
    url = getattr(snap, "url", "")
    text = getattr(snap, "text", "")
    if len(text) > 18000:
        text = text[:18000] + "\n[...truncated]"
    return f"URL: {url}\n{text}"


def _parse_action(content: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "top-level JSON must be an object"
    if "action" not in data:
        return None, "missing 'action' field"
    return data, None


def _parse_and_filter_offers(raw_offers: list[Any]) -> list[Offer]:
    """Validate each offer via the Offer model; drop rows failing the
    grounded-data invariant (price > 0, non-empty url) or Pydantic validation."""
    result: list[Offer] = []
    if not isinstance(raw_offers, list):
        return result
    for row in raw_offers:
        if not isinstance(row, dict):
            continue
        try:
            offer = Offer.model_validate(row)
        except ValidationError:
            continue
        if offer.price <= 0:
            continue
        if not offer.url:
            continue
        result.append(offer)
    return result
