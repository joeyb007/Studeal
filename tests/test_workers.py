"""Tests for the 5 workers + PageReader subagent.

Strategy:
  - StubLLM returns a pre-scripted list of LLM responses. Tests assert that
    each worker:
      * sends the system prompt to the LLM
      * parses the LLM's JSON correctly
      * applies its own enforcement (provenance, scroll budget, etc.)
      * returns the right structured output
  - For PageReader, the StubLLM scripts a sequence of LLM responses, each
    one a JSON {thought, action} dict. Tests verify the loop behavior:
    done termination, scroll budget rejection, loop detection, classified
    retries, action_memory writes on permanent failures.
"""

from __future__ import annotations

from typing import Any

import pytest

from dealbot.agents.perception import ElementRef, PageSnapshot
from dealbot.agents.state import (
    DealOffer,
    FailedAction,
    Finding,
    OrchestratorState,
    Thread,
)
from dealbot.agents.tools import (
    ActionError,
    ActionResult,
    ChangeSummary,
    DomainRateLimiter,
    all_tools,
)
from dealbot.agents.workers import (
    LeadScorer,
    OfferExtractor,
    PageReader,
    SearchPlanner,
    Validator,
)
from dealbot.agents.workers._json_helpers import WorkerOutputError
from dealbot.llm.base import LLMClient, LLMResponse
from dealbot.schemas import WatchlistContext


# ---------------------------------------------------------------------------
# StubLLM
# ---------------------------------------------------------------------------

class StubLLM(LLMClient):
    """Returns the next canned response on each `complete` call.

    Each entry is either a string (raw content) or a callable (msg list →
    string). Tests inspect `self.calls` to verify what was sent.
    """

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.calls.append(messages)
        if not self.responses:
            raise AssertionError("StubLLM ran out of canned responses")
        nxt = self.responses.pop(0)
        content = nxt(messages) if callable(nxt) else nxt
        return LLMResponse(content=content, tool_calls=[])


def _spec(**overrides) -> WatchlistContext:
    defaults = dict(product_query="noise cancelling headphones", keywords=["headphones"])
    defaults.update(overrides)
    return WatchlistContext(**defaults)


# ---------------------------------------------------------------------------
# SearchPlanner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_planner_returns_threads_with_intent_and_url():
    llm = StubLLM(['{"leads": ['
                   '{"intent": "google search", "url": "https://www.google.com/search?q=test"},'
                   '{"intent": "amazon CA", "url": "https://www.amazon.ca/s?k=test"}'
                   ']}'])
    planner = SearchPlanner(llm)
    threads = await planner.plan(_spec())
    assert len(threads) == 2
    assert threads[0].intent == "google search"
    assert threads[0].current_url == "https://www.google.com/search?q=test"
    assert threads[0].depth == 0
    # IDs are populated
    assert all(t.id for t in threads)


@pytest.mark.asyncio
async def test_search_planner_retries_on_bad_json():
    llm = StubLLM([
        "not json at all",                             # parse fails
        '{"leads": [{"intent": "x", "url": "https://x.com"}]}',  # retry succeeds
    ])
    planner = SearchPlanner(llm)
    threads = await planner.plan(_spec())
    assert len(threads) == 1
    assert len(llm.calls) == 2  # retry happened


@pytest.mark.asyncio
async def test_search_planner_raises_after_double_failure():
    llm = StubLLM(["nope", "still nope"])
    planner = SearchPlanner(llm)
    with pytest.raises(WorkerOutputError):
        await planner.plan(_spec())


# ---------------------------------------------------------------------------
# LeadScorer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lead_scorer_returns_clamped_float():
    llm = StubLLM(['{"score": 0.73, "reasoning": "promising new source"}'])
    state = OrchestratorState(spec=_spec())
    lead = Thread(id="t", intent="check bestbuy", current_url="https://bestbuy.ca/x")
    score = await LeadScorer(llm).score(lead, state)
    assert score == 0.73


@pytest.mark.asyncio
async def test_lead_scorer_rejects_out_of_range_score():
    """Pydantic ge=0 le=1 should reject out-of-range scores; helper retries."""
    llm = StubLLM([
        '{"score": 1.5, "reasoning": "way too high"}',   # invalid: >1
        '{"score": 0.5, "reasoning": "ok"}',
    ])
    state = OrchestratorState(spec=_spec())
    lead = Thread(id="t", intent="x", current_url="https://x.com")
    score = await LeadScorer(llm).score(lead, state)
    assert score == 0.5
    assert len(llm.calls) == 2


# ---------------------------------------------------------------------------
# OfferExtractor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_offer_extractor_returns_offers_with_observation_provenance():
    llm = StubLLM(['{"offers": [{'
                   '"title": "Sony WH-1000XM5",'
                   '"price": 199.99, "price_provenance": "observation",'
                   '"listed_price": 249.99, "listed_price_provenance": "observation",'
                   '"url": "https://amazon.ca/dp/x", "url_provenance": "observation",'
                   '"retailer": "Amazon CA", "condition": "new"'
                   '}]}'])
    thread = Thread(
        id="t", intent="x",
        findings=[Finding(text="saw $199.99 on amazon", provenance="observation")],
    )
    offers = await OfferExtractor(llm).extract(thread, _spec())
    assert len(offers) == 1
    assert offers[0].price == 199.99
    assert offers[0].price_provenance == "observation"
    assert offers[0].url_provenance == "observation"


@pytest.mark.asyncio
async def test_offer_extractor_drops_inference_priced_offers():
    """Even if the LLM says inference, the worker hard-filters them out."""
    llm = StubLLM(['{"offers": [{'
                   '"title": "X",'
                   '"price": 200, "price_provenance": "inference",'
                   '"url": "https://x.com", "url_provenance": "observation",'
                   '"retailer": "Z", "condition": "new"'
                   '}]}'])
    thread = Thread(id="t", intent="x", findings=[
        Finding(text="probably $200", provenance="inference"),
    ])
    offers = await OfferExtractor(llm).extract(thread, _spec())
    assert offers == []


@pytest.mark.asyncio
async def test_offer_extractor_returns_empty_for_no_findings():
    llm = StubLLM([])   # no call should happen
    thread = Thread(id="t", intent="x", findings=[])
    offers = await OfferExtractor(llm).extract(thread, _spec())
    assert offers == []
    assert llm.calls == []  # short-circuited without an LLM call


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validator_keeps_indicated_offers():
    llm = StubLLM(['{'
                   '"acceptable": true,'
                   '"kept_offer_indices": [0, 2],'
                   '"feedback": "looks good",'
                   '"suggested_leads": []'
                   '}'])
    offers = [
        DealOffer(title="A", price=100, price_provenance="observation",
                  url="https://a.com", url_provenance="observation", retailer="A"),
        DealOffer(title="B", price=150, price_provenance="observation",
                  url="https://b.com", url_provenance="observation", retailer="B"),
        DealOffer(title="C", price=200, price_provenance="observation",
                  url="https://c.com", url_provenance="observation", retailer="C"),
    ]
    decision = await Validator(llm).validate(_spec(), offers)
    assert decision.acceptable
    assert [o.title for o in decision.kept_offers] == ["A", "C"]


@pytest.mark.asyncio
async def test_validator_emits_suggested_leads_for_replan():
    llm = StubLLM(['{'
                   '"acceptable": false,'
                   '"kept_offer_indices": [],'
                   '"feedback": "missing canadian retailers",'
                   '"suggested_leads": ['
                   '{"intent": "check bestbuy.ca", "url": "https://www.bestbuy.ca/x"}'
                   ']}'])
    offers = [
        DealOffer(title="A", price=100, price_provenance="observation",
                  url="https://a.com", url_provenance="observation", retailer="A"),
    ]
    decision = await Validator(llm).validate(_spec(), offers)
    assert not decision.acceptable
    assert len(decision.suggested_leads) == 1
    assert decision.suggested_leads[0].intent == "check bestbuy.ca"


@pytest.mark.asyncio
async def test_validator_handles_zero_offers_without_llm_call():
    llm = StubLLM([])
    decision = await Validator(llm).validate(_spec(), [])
    assert not decision.acceptable
    assert decision.kept_offers == []


# ---------------------------------------------------------------------------
# PageReader subagent
# ---------------------------------------------------------------------------

class _MockMouse:
    def __init__(self) -> None:
        self.clicks: list[tuple[float, float]] = []
        self.wheel_calls: list[tuple[int, int]] = []

    async def click(self, x: float, y: float) -> None:
        self.clicks.append((x, y))

    async def wheel(self, dx: int, dy: int) -> None:
        self.wheel_calls.append((dx, dy))


class _MockKeyboard:
    async def type(self, text: str) -> None: pass
    async def press(self, key: str) -> None: pass


class _MockWatchdog:
    async def wait_for_settlement(self, *args, **kwargs) -> None: pass


class _MockSession:
    def __init__(self, page) -> None:
        self.page = page
        self.watchdog = _MockWatchdog()
        self.intercepted_responses: list = []


class _MockPage:
    def __init__(self, url: str = "https://start/") -> None:
        self.url = url
        self.mouse = _MockMouse()
        self.keyboard = _MockKeyboard()

    async def goto(self, url: str, **kw): self.url = url
    async def title(self): return "mock"


def _snapshot_with(ids: list[int], url: str = "https://x.com/") -> PageSnapshot:
    return PageSnapshot(
        text="<mock>",
        element_map={
            i: ElementRef(
                backend_node_id=i, role="button", name=f"btn-{i}",
                tag_name="button", bbox=(10.0 + i * 5, 10.0, 50.0, 30.0),
                is_interactive=True,
            ) for i in ids
        },
        url=url, title="mock", char_count=10,
    )


@pytest.mark.asyncio
async def test_page_reader_done_action_terminates(monkeypatch):
    """LLM emits done on turn 1; loop exits cleanly."""
    llm = StubLLM([
        '{"thought": "starting fresh", "action": {"type": "done", "reason": "no relevant content"}}',
    ])
    snaps = [_snapshot_with([1, 2])]
    async def fake_snap(p): return snaps.pop(0)
    monkeypatch.setattr("dealbot.agents.workers.page_reader.snapshot_page", fake_snap)
    monkeypatch.setattr("dealbot.agents.tools.snapshot_page", fake_snap)

    pr = PageReader(llm, tools=all_tools())
    page = _MockPage()
    session = _MockSession(page)
    state = OrchestratorState(spec=_spec())
    thread = Thread(id="t", intent="explore", current_url="https://x.com/")

    result = await pr.explore(thread, session, state, DomainRateLimiter(0.001))

    assert result.stop_reason == "done"
    assert result.turns_used == 1
    assert result.summary == "no relevant content"


@pytest.mark.asyncio
async def test_page_reader_max_turns_exhausted(monkeypatch):
    """LLM never calls done; loop exits at max_turns."""
    # Respond with read_page every turn — keeps going forever.
    llm = StubLLM([
        '{"thought": "read", "action": {"type": "read_page"}}'
    ] * 20)

    # Vary URL each snapshot so loop detection doesn't fire
    counter = {"i": 0}
    def make_snap():
        counter["i"] += 1
        return _snapshot_with([counter["i"], counter["i"] + 100], url=f"https://x.com/{counter['i']}")

    async def fake_snap(p):
        return make_snap()
    monkeypatch.setattr("dealbot.agents.workers.page_reader.snapshot_page", fake_snap)
    monkeypatch.setattr("dealbot.agents.tools.snapshot_page", fake_snap)

    pr = PageReader(llm, tools=all_tools(), max_turns=4)
    page = _MockPage()
    session = _MockSession(page)
    state = OrchestratorState(spec=_spec())
    thread = Thread(id="t", intent="x", current_url="https://x.com/")

    result = await pr.explore(thread, session, state, DomainRateLimiter(0.001))
    assert result.stop_reason == "max_turns"
    assert result.turns_used == 4


@pytest.mark.asyncio
async def test_page_reader_loop_detection(monkeypatch):
    """Three consecutive identical snapshots → loop stop."""
    llm = StubLLM([
        '{"thought": "read", "action": {"type": "read_page"}}'
    ] * 5)

    fixed_snap = _snapshot_with([1, 2, 3])
    async def fake_snap(p): return fixed_snap
    monkeypatch.setattr("dealbot.agents.workers.page_reader.snapshot_page", fake_snap)
    monkeypatch.setattr("dealbot.agents.tools.snapshot_page", fake_snap)

    pr = PageReader(llm, tools=all_tools())
    page = _MockPage()
    session = _MockSession(page)
    state = OrchestratorState(spec=_spec())
    thread = Thread(id="t", intent="x", current_url="https://x.com/")

    result = await pr.explore(thread, session, state, DomainRateLimiter(0.001))
    assert result.stop_reason == "loop"


@pytest.mark.asyncio
async def test_page_reader_scroll_budget_enforced(monkeypatch):
    """6th scroll is rejected before tool dispatch; loop continues."""
    llm = StubLLM([
        '{"thought": "s1", "action": {"type": "scroll", "direction": "down"}}',
        '{"thought": "s2", "action": {"type": "scroll", "direction": "down"}}',
        '{"thought": "s3", "action": {"type": "scroll", "direction": "down"}}',
        '{"thought": "s4", "action": {"type": "scroll", "direction": "down"}}',
        '{"thought": "s5", "action": {"type": "scroll", "direction": "down"}}',
        '{"thought": "s6", "action": {"type": "scroll", "direction": "down"}}',
        '{"thought": "done", "action": {"type": "done", "reason": "budget"}}',
    ])

    counter = {"i": 0}
    async def fake_snap(p):
        counter["i"] += 1
        return _snapshot_with([counter["i"]], url=f"https://x.com/{counter['i']}")
    monkeypatch.setattr("dealbot.agents.workers.page_reader.snapshot_page", fake_snap)
    monkeypatch.setattr("dealbot.agents.tools.snapshot_page", fake_snap)

    pr = PageReader(llm, tools=all_tools())
    page = _MockPage()
    session = _MockSession(page)
    state = OrchestratorState(spec=_spec())
    thread = Thread(id="t", intent="x", current_url="https://x.com/")

    result = await pr.explore(thread, session, state, DomainRateLimiter(0.001))
    # 5 scrolls executed (wheel called 5x), 6th rejected, then done
    assert len(page.mouse.wheel_calls) == 5
    assert result.stop_reason == "done"


@pytest.mark.asyncio
async def test_page_reader_action_memory_injected_into_initial_prompt(monkeypatch):
    """action_memory entries for the current URL show up in the first user msg."""
    llm = StubLLM([
        '{"thought": "done", "action": {"type": "done", "reason": "ok"}}',
    ])
    async def fake_snap(p): return _snapshot_with([1])
    monkeypatch.setattr("dealbot.agents.workers.page_reader.snapshot_page", fake_snap)
    monkeypatch.setattr("dealbot.agents.tools.snapshot_page", fake_snap)

    pr = PageReader(llm, tools=all_tools())
    page = _MockPage(url="https://amazon.ca/dp/x")
    session = _MockSession(page)
    state = OrchestratorState(spec=_spec())
    state.action_memory["https://amazon.ca/dp/x"] = [
        FailedAction(
            tool="click", args_summary='{"element_id": 42}',
            error_type="not_found", turn=3,
        ),
    ]
    thread = Thread(id="t", intent="x", current_url="https://amazon.ca/dp/x")

    await pr.explore(thread, session, state, DomainRateLimiter(0.001))

    # Initial user msg (index 1) should contain the failed-action warning
    initial_user = llm.calls[0][1]["content"]
    assert "click({\"element_id\": 42})" in initial_user
    assert "not_found" in initial_user


@pytest.mark.asyncio
async def test_page_reader_permanent_error_writes_to_action_memory(monkeypatch):
    """Click on a non-existent element_id → not_found → action_memory grows."""
    llm = StubLLM([
        '{"thought": "click missing", "action": {"type": "click", "element_id": 999}}',
        '{"thought": "give up", "action": {"type": "done", "reason": "blocked"}}',
    ])

    counter = {"i": 0}
    async def fake_snap(p):
        counter["i"] += 1
        return _snapshot_with([counter["i"]], url=f"https://amazon.ca/dp/y")
    monkeypatch.setattr("dealbot.agents.workers.page_reader.snapshot_page", fake_snap)
    monkeypatch.setattr("dealbot.agents.tools.snapshot_page", fake_snap)

    pr = PageReader(llm, tools=all_tools())
    page = _MockPage(url="https://amazon.ca/dp/y")
    session = _MockSession(page)
    state = OrchestratorState(spec=_spec())
    thread = Thread(id="t", intent="x", current_url="https://amazon.ca/dp/y")

    result = await pr.explore(thread, session, state, DomainRateLimiter(0.001))

    assert result.stop_reason == "done"
    # action_memory should now have an entry for the failed click
    assert "https://amazon.ca/dp/y" in state.action_memory
    failed = state.action_memory["https://amazon.ca/dp/y"]
    assert len(failed) == 1
    assert failed[0].tool == "click"
    assert failed[0].error_type == "not_found"


@pytest.mark.asyncio
async def test_page_reader_bad_json_response_does_not_crash(monkeypatch):
    """Malformed LLM output → corrective message back to LLM, no crash."""
    llm = StubLLM([
        "not json",
        '{"thought": "ok", "action": {"type": "done", "reason": "fine"}}',
    ])

    counter = {"i": 0}
    async def fake_snap(p):
        counter["i"] += 1
        return _snapshot_with([counter["i"]], url=f"https://x.com/{counter['i']}")
    monkeypatch.setattr("dealbot.agents.workers.page_reader.snapshot_page", fake_snap)
    monkeypatch.setattr("dealbot.agents.tools.snapshot_page", fake_snap)

    pr = PageReader(llm, tools=all_tools())
    page = _MockPage()
    session = _MockSession(page)
    state = OrchestratorState(spec=_spec())
    thread = Thread(id="t", intent="x", current_url="https://x.com/")

    result = await pr.explore(thread, session, state, DomainRateLimiter(0.001))
    assert result.stop_reason == "done"
    # Both LLM calls happened — bad-json correction worked
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_page_reader_records_finding_to_thread(monkeypatch):
    llm = StubLLM([
        '{"thought": "record price", "action": {'
        '"type": "record_finding", "text": "Sony XM5 = $199",'
        '"provenance": "observation", "source_url": "https://amazon.ca/dp/x"'
        '}}',
        '{"thought": "done", "action": {"type": "done", "reason": "found price"}}',
    ])
    counter = {"i": 0}
    async def fake_snap(p):
        counter["i"] += 1
        return _snapshot_with([counter["i"]], url=f"https://amazon.ca/dp/x")
    monkeypatch.setattr("dealbot.agents.workers.page_reader.snapshot_page", fake_snap)
    monkeypatch.setattr("dealbot.agents.tools.snapshot_page", fake_snap)

    pr = PageReader(llm, tools=all_tools())
    page = _MockPage(url="https://amazon.ca/dp/x")
    session = _MockSession(page)
    state = OrchestratorState(spec=_spec())
    thread = Thread(id="t", intent="x", current_url="https://amazon.ca/dp/x")

    result = await pr.explore(thread, session, state, DomainRateLimiter(0.001))
    assert len(result.findings_added) == 1
    assert result.findings_added[0].provenance == "observation"
    assert "Sony XM5 = $199" in result.findings_added[0].text
