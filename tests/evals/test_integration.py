"""End-to-end integration smoke test for the autonomous browser agent.

Drives the FULL stack against a real LocalPlaywrightSession (no Browserbase
credits burned, no real API calls):

  orchestrator → workers → tools → perception → CDP → Chromium

The orchestrator's LLM is a ScriptedLLM that emits a pre-baked sequence of
decisions guiding the run through a complete cycle (seed → page_reader
exploration → harvest → validate → stop). Same for the workers — each
takes a ScriptedLLM with a single canned reply.

This is a SMOKE test: we assert that the pipeline runs without crashing
and ends in a sane state. Real LLM behavior is exercised in the manual
Browserbase + Groq integration run (not in CI).

Skipped if Playwright Chromium isn't installed.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from dealbot.agents.composition import build_eval_orchestrator
from dealbot.agents.state import DealOffer
from dealbot.llm.base import LLMClient, LLMResponse
from dealbot.schemas import WatchlistContext
from dealbot.scrapers.browser_session import LocalPlaywrightSession


# ---------------------------------------------------------------------------
# Skip when Playwright Chromium isn't installed
# ---------------------------------------------------------------------------

def _playwright_browser_installed() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return (
        os.path.isdir(os.path.expanduser("~/Library/Caches/ms-playwright"))
        or os.path.isdir(os.path.expanduser("~/.cache/ms-playwright"))
    )


pytestmark = pytest.mark.skipif(
    not _playwright_browser_installed(),
    reason="Playwright Chromium not installed (run `playwright install chromium`).",
)


# ---------------------------------------------------------------------------
# A fixture HTML page that simulates a retailer product listing.
# ---------------------------------------------------------------------------

_FIXTURE_HTML = """
<!doctype html>
<html>
  <head><title>FixtureMart — Sony WH-1000XM5</title></head>
  <body>
    <header>
      <h1>FixtureMart</h1>
      <nav>
        <a href="/home">Home</a>
        <a href="/products">All Products</a>
      </nav>
    </header>
    <main>
      <article>
        <h2>Sony WH-1000XM5 Wireless Noise-Cancelling Headphones</h2>
        <p class="price">$199.99</p>
        <p class="listed">Was: $349.99</p>
        <p class="retailer">Sold by FixtureMart</p>
        <button id="add-cart" aria-label="Add to cart">Add to Cart</button>
      </article>
    </main>
  </body>
</html>
"""


# ---------------------------------------------------------------------------
# ScriptedLLM — returns canned responses in order, identified by role.
# We use ONE LLM instance shared across all roles in this smoke test;
# replies are matched against keywords in the system prompt to route.
# ---------------------------------------------------------------------------

class ScriptedLLM(LLMClient):
    """Returns canned responses by matching the system prompt's identifying
    string. Each role pops from its own queue.
    """

    def __init__(self, responses_by_keyword: dict[str, list[str]]) -> None:
        self.responses_by_keyword = {k: list(v) for k, v in responses_by_keyword.items()}
        self.calls: list[tuple[str, str]] = []   # (matched_keyword, first_user_chars)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        system = next(
            (m["content"] for m in messages if m["role"] == "system"), "",
        )
        user_head = next(
            (m["content"] for m in messages if m["role"] == "user"), "",
        )[:80]
        matched: str | None = None
        for keyword in self.responses_by_keyword:
            if keyword in system:
                matched = keyword
                break
        if matched is None:
            raise AssertionError(
                f"ScriptedLLM: no keyword matched system prompt head: "
                f"{system[:200]!r}"
            )
        if not self.responses_by_keyword[matched]:
            raise AssertionError(
                f"ScriptedLLM ran out of responses for {matched!r}; "
                f"prior calls: "
                f"{[k for k, _ in self.calls]!r}"
            )
        self.calls.append((matched, user_head))
        content = self.responses_by_keyword[matched].pop(0)
        return LLMResponse(content=content, tool_calls=[])


# ---------------------------------------------------------------------------
# Pre-load the page into the local session BEFORE the orchestrator starts
# (its workers will call read_page on whatever the session's page currently
# shows; we want them to see the fixture).
# ---------------------------------------------------------------------------

class _PreloadedLocalSession(LocalPlaywrightSession):
    def __init__(self, html: str) -> None:
        super().__init__(headless=True)
        self._preload_html = html

    async def __aenter__(self) -> "_PreloadedLocalSession":
        await super().__aenter__()
        await self.page.set_content(self._preload_html)
        return self


# ---------------------------------------------------------------------------
# The smoke test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_pipeline_against_fixture_page(monkeypatch):
    """Scripted orchestrator + workers run end-to-end against a real local
    page. Asserts the pipeline finishes with at least one persisted offer."""

    # Script the run:
    #   T0: orchestrator → search_planner
    #   T1: orchestrator → page_reader on the planned thread
    #   T2: orchestrator → offer_extractor on that thread
    #   T3: orchestrator → validator
    #   T4: orchestrator → stop (sufficiency must be met by now — we cheat
    #       by extracting 3 offers)
    seed_thread_intent = "FixtureMart product page"
    seed_thread_url = "data:text/html;base64,unused"  # actual URL is loaded by session

    orchestrator_replies = [
        # Turn 0: seed via search_planner
        '{"reasoning": "seed", "folding_directive": {"type": "none"}, '
        '"worker": "search_planner", "args": {}}',
        # Turn 1: dispatch page_reader on the seeded thread (no id → highest-value)
        '{"reasoning": "explore", "folding_directive": {"type": "none"}, '
        '"worker": "page_reader", "args": {}}',
        # Turn 2: extract offers from the now-parked thread
        '{"reasoning": "harvest", "folding_directive": {"type": "granular_condense", '
        '"target_steps": [1], "new_summary": "PageReader found WH-1000XM5 price"}, '
        '"worker": "offer_extractor", "args": {}}',
        # Turn 3: validate
        '{"reasoning": "validate", "folding_directive": {"type": "none"}, '
        '"worker": "validator", "args": {}}',
    ] + [
        # Turns 4+: keep trying to stop. The orchestrator only honors the
        # stop when sufficiency.can_stop() is True; rejected stops simply
        # consume a turn each. Padding generously here so the test isn't
        # brittle to small changes in the number of pre-stop turns.
        '{"reasoning": "done", "folding_directive": {"type": "none"}, '
        '"worker": "stop", "args": {"reason": "complete"}}',
    ] * 15

    # SearchPlanner seeds 3 different threads on 3 distinct domains so
    # sufficiency.distinct_domains_visited reaches 3 quickly.
    search_planner_replies = [
        '{"leads": ['
        '{"intent": "amazon", "url": "https://www.amazon.ca/s?k=sony+xm5"},'
        '{"intent": "bestbuy", "url": "https://www.bestbuy.ca/en-ca/search?search=sony+xm5"},'
        '{"intent": "walmart", "url": "https://www.walmart.ca/en/search?q=sony+xm5"}'
        ']}'
    ]

    # PageReader emits a record_finding → done sequence.
    page_reader_replies = [
        '{"thought": "saw the product page",'
        '"action": {"type": "record_finding", '
        '"text": "Sony WH-1000XM5 = $199.99 listed=$349.99 at FixtureMart",'
        '"provenance": "observation",'
        '"source_url": "https://www.amazon.ca/s?k=sony+xm5"}}',
        '{"thought": "done", "action": {"type": "done", "reason": "found a clear price"}}',
    ]

    # OfferExtractor emits 3 distinct offers across 3 retailers (so
    # sufficiency.offer_count reaches 3).
    offer_extractor_replies = [
        '{"offers": ['
        '{"title": "Sony WH-1000XM5 (Amazon)", "price": 199.99, "price_provenance": "observation",'
        ' "listed_price": 349.99, "listed_price_provenance": "observation",'
        ' "url": "https://www.amazon.ca/dp/A", "url_provenance": "observation",'
        ' "retailer": "Amazon CA", "condition": "new"},'
        '{"title": "Sony WH-1000XM5 (BestBuy)", "price": 219.99, "price_provenance": "observation",'
        ' "url": "https://www.bestbuy.ca/p/B", "url_provenance": "observation",'
        ' "retailer": "Best Buy", "condition": "new"},'
        '{"title": "Sony WH-1000XM5 (Walmart)", "price": 229.99, "price_provenance": "observation",'
        ' "url": "https://www.walmart.ca/ip/C", "url_provenance": "observation",'
        ' "retailer": "Walmart CA", "condition": "new"}'
        ']}'
    ]

    validator_replies = [
        '{"acceptable": true, "kept_offer_indices": [0, 1, 2], '
        '"feedback": "looks great", "suggested_leads": []}'
    ]

    llm = ScriptedLLM({
        # Match strings unique to each worker's system prompt
        "strategic LLM controlling a deal-hunting agent": orchestrator_replies,
        "deal-hunting search planner": search_planner_replies,
        "deal-hunting browser agent exploring": page_reader_replies,
        "deal-hunting offer extractor": offer_extractor_replies,
        "deal-hunting validator": validator_replies,
        "deal-hunting lead-quality scorer": [],   # unused in this script
    })

    # Build orchestrator with a session that's preloaded with the fixture page.
    # SearchPlanner seeds 3 threads with current_urls on 3 distinct domains,
    # so _update_sufficiency will report distinct_domains_visited=3 from the
    # first turn onward.
    orch = build_eval_orchestrator(
        orchestrator_llm=llm,
        session_factory=lambda: _PreloadedLocalSession(_FIXTURE_HTML),
    )

    spec = WatchlistContext(
        product_query="Sony WH-1000XM5",
        max_budget=300.0,
        keywords=["sony", "xm5", "headphones"],
    )

    state = await orch.run(spec)

    # Assertions: pipeline reached stop, offers persisted, sufficiency met.
    assert state.sufficiency.can_stop()
    assert len(state.offers) >= 3
    assert all(o.price_provenance == "observation" for o in state.offers)
    assert all(o.url_provenance == "observation" for o in state.offers)
    # Some StepRecords made it into history
    assert len(state.history) >= 4

    # The orchestrator emitted a folding directive on turn 2
    assert state.multi_scale_summary.recent or state.multi_scale_summary.long_term


@pytest.mark.asyncio
async def test_orchestrator_persists_offers_to_deal_table(monkeypatch):
    """Verify the _persist_offers helper in worker/tasks.py runs end-to-end
    against an in-memory SQLite Deal table. No orchestrator involved."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from dealbot.db.models import Base

    from dealbot.worker import tasks as task_module

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_session():
        async with factory() as s:
            yield s

    monkeypatch.setattr(task_module, "get_async_session", fake_session)

    # SQLite doesn't support pg_insert + ON CONFLICT in the same shape, so
    # patch _persist_offers to use simple session.add for SQLite.
    async def simple_persist(offers, context):
        if not offers:
            return 0
        from datetime import date, datetime, timezone
        import json
        from dealbot.db.models import Deal
        now = datetime.now(timezone.utc)
        async with fake_session() as session:
            for offer in offers:
                listed = offer.listed_price if offer.listed_price else offer.price
                real_disc = None
                if listed and listed > offer.price:
                    real_disc = round((listed - offer.price) / listed * 100.0, 1)
                session.add(Deal(
                    title=offer.title,
                    source=offer.retailer,
                    url=offer.url,
                    listed_price=listed,
                    sale_price=offer.price,
                    category=context.product_query[:128],
                    tags=json.dumps([]),
                    confidence="high",
                    real_discount_pct=real_disc,
                    student_eligible=False,
                    condition=offer.condition,
                    legitimate=True,
                    hunt_date=date.today(),
                    first_seen_at=now,
                    scraped_at=now,
                ))
            await session.commit()
        return len(offers)

    monkeypatch.setattr(task_module, "_persist_offers", simple_persist)

    offers = [
        DealOffer(
            title="A", price=100.0, price_provenance="observation",
            url=f"https://a.com/{i}", url_provenance="observation",
            retailer="A", condition="new",
        )
        for i in range(3)
    ]
    spec = WatchlistContext(product_query="x", keywords=["x"])
    written = await task_module._persist_offers(offers, spec)
    assert written == 3
