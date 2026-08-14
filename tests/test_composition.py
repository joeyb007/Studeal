"""Tests for nav/extract LLM builder split."""
import asyncio
import pytest

from dealbot.agents.composition import build_extract_llm, build_nav_llm, ThrottledLLM
from dealbot.llm.groq_client import GroqClient
from dealbot.llm.openai_client import OpenAIClient


def test_nav_llm_defaults_to_gpt4o(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("AGENT_NAV_MODEL", raising=False)
    llm = build_nav_llm()
    assert isinstance(llm, ThrottledLLM)
    assert isinstance(llm.inner, OpenAIClient)
    assert llm.inner.model == "gpt-4o"


def test_nav_llm_respects_env_override(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("AGENT_NAV_MODEL", "gpt-4.1")
    llm = build_nav_llm()
    assert llm.inner.model == "gpt-4.1"


def test_extract_llm_defaults_to_mini(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    llm = build_extract_llm()
    assert isinstance(llm, OpenAIClient)
    assert llm.model == "gpt-4o-mini"


def test_nav_llms_fast_pair_shares_one_thinking_budget(monkeypatch):
    from dealbot.agents.composition import build_nav_llms
    from dealbot.llm.bedrock_client import DEFAULT_NAV_FAST_MODEL, DEFAULT_NAV_MODEL

    monkeypatch.setenv("LLM_BACKEND", "bedrock")
    monkeypatch.delenv("AGENT_NAV_FAST", raising=False)
    primary, escalation = build_nav_llms()
    assert primary.inner.model == DEFAULT_NAV_FAST_MODEL
    assert escalation.inner.model == DEFAULT_NAV_MODEL
    assert primary._sem is escalation._sem, "tiers must share one semaphore"


def test_nav_llms_rollback_lever_restores_single_frontier(monkeypatch):
    from dealbot.agents.composition import build_nav_llms
    from dealbot.llm.bedrock_client import DEFAULT_NAV_MODEL

    monkeypatch.setenv("LLM_BACKEND", "bedrock")
    monkeypatch.setenv("AGENT_NAV_FAST", "0")
    primary, escalation = build_nav_llms()
    assert escalation is None
    assert primary.inner.model == DEFAULT_NAV_MODEL


def test_builders_fall_back_to_groq(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    nav_llm = build_nav_llm()
    extract_llm = build_extract_llm()
    assert isinstance(nav_llm, ThrottledLLM)
    assert isinstance(nav_llm.inner, GroqClient)
    assert isinstance(extract_llm, GroqClient)


class _SlowLLM:
    supports_vision = True

    def __init__(self):
        self.active = 0
        self.peak = 0

    async def complete(self, messages, tools=None, response_format=None):
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return None


@pytest.mark.asyncio
async def test_throttled_llm_caps_concurrency():
    inner = _SlowLLM()
    llm = ThrottledLLM(inner, max_concurrency=2)
    await asyncio.gather(*(llm.complete([]) for _ in range(8)))
    assert inner.peak <= 2


def test_throttled_llm_forwards_supports_vision():
    assert ThrottledLLM(_SlowLLM(), max_concurrency=1).supports_vision is True


def test_nav_llm_is_throttled(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("AGENT_NAV_CONCURRENCY", "3")
    llm = build_nav_llm()
    assert isinstance(llm, ThrottledLLM)


def test_throttled_llm_forwards_usage_counters():
    class _CountingLLM:
        supports_vision = False
        total_prompt_tokens = 120
        total_completion_tokens = 30
        call_count = 4

        async def complete(self, messages, tools=None, response_format=None):
            return None

    t = ThrottledLLM(_CountingLLM(), max_concurrency=1)
    assert t.total_prompt_tokens == 120
    assert t.total_completion_tokens == 30
    assert t.call_count == 4


def test_openai_client_record_usage():
    """Test counter accumulation via _record_usage helper directly."""
    from dealbot.llm.openai_client import OpenAIClient

    client = OpenAIClient(model="gpt-4o", api_key="sk-test")
    assert client.total_prompt_tokens == 0
    assert client.total_completion_tokens == 0
    assert client.call_count == 0

    # Call twice with valid usage dicts — expect totals to accumulate.
    client._record_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    client._record_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    assert client.total_prompt_tokens == 20
    assert client.total_completion_tokens == 10
    assert client.call_count == 2

    # Missing usage key — must silently no-op, not raise.
    client._record_usage({})
    assert client.call_count == 2

    # Malformed / unexpected types — must silently no-op, not raise.
    client._record_usage({"usage": "not-a-dict"})
    client._record_usage({"usage": {"prompt_tokens": "bad", "completion_tokens": None}})
    assert client.call_count == 2


# ---------------------------------------------------------------------
# A3: no-results query broadening in _run_one_query
# ---------------------------------------------------------------------

import pytest as _pytest

from dealbot.agents.composition import _run_one_query
from dealbot.agents.explorer import ExplorerResult
from dealbot.agents.marketplace_router import MarketplaceSearchTarget
from dealbot.schemas import WatchlistContext


class _FakeRouter:
    """Returns one kijiji target whose entry_url embeds the routed query."""

    def __init__(self):
        self.routed_queries: list[str] = []

    async def route(self, query, spec):
        self.routed_queries.append(query)
        return [MarketplaceSearchTarget(
            marketplace="kijiji",
            entry_url=f"https://kijiji.test/search?q={query.replace(' ', '+')}",
        )]


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    page = None


class _ScriptedExplorer:
    """Replaces Explorer inside composition; records explore() calls and pops
    scripted results."""

    calls: list[tuple[str, str]] = []       # (query, entry_url)
    results: list[ExplorerResult] = []

    def __init__(self, llm, trace=None, escalation_llm=None):
        pass

    async def explore(self, entry_url, marketplace, query, spec, session, sink, entry_referer=None):
        _ScriptedExplorer.calls.append((query, entry_url))
        return _ScriptedExplorer.results.pop(0)


class _NullPool:
    async def submit(self, snap, marketplace, spec):
        pass


@_pytest.fixture()
def composition_rig(monkeypatch):
    import dealbot.agents.composition as comp

    monkeypatch.setattr(comp, "Explorer", _ScriptedExplorer)
    monkeypatch.setattr(
        comp, "build_session_from_env",
        lambda backend=None, proxies=True: _FakeSession(),
    )
    _ScriptedExplorer.calls = []
    _ScriptedExplorer.results = []
    return comp


@_pytest.mark.asyncio
async def test_no_results_retries_with_core_query(composition_rig):
    spec = WatchlistContext(product_query="aeron chair")
    router = _FakeRouter()
    _ScriptedExplorer.results = [
        ExplorerResult(stop_reason="done", done_reason="no_results for this query"),
        ExplorerResult(stop_reason="done", done_reason="done after retry"),
    ]
    await _run_one_query("used aeron chair herman miller", spec, router, None, _NullPool(), asyncio.Semaphore(6))

    assert len(_ScriptedExplorer.calls) == 2
    assert _ScriptedExplorer.calls[0][0] == "used aeron chair herman miller"
    assert _ScriptedExplorer.calls[1][0] == "aeron chair"          # core query
    assert "aeron+chair" in _ScriptedExplorer.calls[1][1]          # re-routed URL
    assert router.routed_queries == ["used aeron chair herman miller", "aeron chair"]


@_pytest.mark.asyncio
async def test_no_retry_when_query_already_core(composition_rig):
    spec = WatchlistContext(product_query="aeron chair")
    router = _FakeRouter()
    _ScriptedExplorer.results = [
        ExplorerResult(stop_reason="done", done_reason="no_results"),
    ]
    await _run_one_query("aeron chair", spec, router, None, _NullPool(), asyncio.Semaphore(6))
    assert len(_ScriptedExplorer.calls) == 1


@_pytest.mark.asyncio
async def test_no_retry_on_normal_done(composition_rig):
    spec = WatchlistContext(product_query="aeron chair")
    router = _FakeRouter()
    _ScriptedExplorer.results = [
        ExplorerResult(stop_reason="done", done_reason="paginated fully"),
    ]
    await _run_one_query("used aeron chair", spec, router, None, _NullPool(), asyncio.Semaphore(6))
    assert len(_ScriptedExplorer.calls) == 1


# ---------------------------------------------------------------------
# Whole-hunt browse deadline: stuck lanes get cancelled, extracted
# offers are still drained and returned.
# ---------------------------------------------------------------------


@_pytest.mark.asyncio
async def test_run_hunt_browse_deadline_salvages_offers(composition_rig, monkeypatch):
    comp = composition_rig
    monkeypatch.setenv("HUNT_BROWSE_DEADLINE_S", "0")

    async def stuck_query(*args, **kwargs):
        await asyncio.Event().wait()        # a lane that never finishes

    class _FakeGen:
        def __init__(self, llm):
            pass

        async def generate(self, spec):
            return ["aeron chair"]

    class _DrainPool:
        def __init__(self, extractor, num_workers=3):
            pass

        async def start(self):
            pass

        async def drain(self):
            return ["salvaged-offer"]

    monkeypatch.setattr(comp, "build_nav_llms", lambda: (None, None))
    monkeypatch.setattr(comp, "build_extract_llm", lambda: None)
    monkeypatch.setattr(comp, "Extractor", lambda llm: None)
    monkeypatch.setattr(comp, "ExtractorPool", _DrainPool)
    monkeypatch.setattr(comp, "MarketplaceRouter", lambda llm: None)
    monkeypatch.setattr(comp, "QueryGenerator", _FakeGen)
    monkeypatch.setattr(comp, "_run_one_query", stuck_query)

    offers = await asyncio.wait_for(
        comp.run_hunt(WatchlistContext(product_query="aeron chair")), timeout=5,
    )
    assert offers == ["salvaged-offer"]
