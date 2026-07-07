"""Extractor — page-locked LLM sub-worker with fresh context per invocation.

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

from typing import Literal

from playwright.async_api import Page
from pydantic import BaseModel

from dealbot.llm.base import LLMClient
from dealbot.schemas import WatchlistContext


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

        See module docstring for full contract. Not implemented yet."""
        raise NotImplementedError("Day 1 impl pending — interface only")
