"""Tests for MarketplaceRouter."""

from __future__ import annotations

import json

import pytest

from dealbot.agents.marketplace_router import (
    CURATED_MARKETPLACES,
    MarketplaceConfig,
    MarketplaceRouter,
)
from dealbot.schemas import WatchlistContext


class _MockResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _MockLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete(self, messages, response_format=None, **kwargs):
        if self._responses:
            return _MockResponse(self._responses.pop(0))
        return _MockResponse('{"marketplaces":[]}')


class _RaisingLLM:
    async def complete(self, messages, response_format=None, **kwargs):
        raise RuntimeError("LLM down")


def _spec() -> WatchlistContext:
    return WatchlistContext(product_query="Aeron", max_budget=700.0)


# Two mocked marketplaces for controlled tests.
_M_A = MarketplaceConfig(
    key="alpha", display_name="Alpha", description="fake alpha",
    home_url="https://alpha.test",
)
_M_B = MarketplaceConfig(
    key="beta", display_name="Beta", description="fake beta",
    home_url="https://beta.test",
)


# ---------------------------------------------------------------------
# Tests — routing behavior
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_routes_to_llm_picked_subset():
    llm = _MockLLM([json.dumps({"marketplaces": ["alpha"]})])
    router = MarketplaceRouter(llm=llm, marketplaces=[_M_A, _M_B])
    targets = await router.route("aeron chair", _spec())

    assert len(targets) == 1
    assert targets[0].marketplace == "alpha"
    assert targets[0].entry_url == "https://alpha.test"


@pytest.mark.asyncio
async def test_ignores_unknown_marketplace_keys_from_llm():
    llm = _MockLLM([json.dumps({"marketplaces": ["alpha", "hallucinated"]})])
    router = MarketplaceRouter(llm=llm, marketplaces=[_M_A, _M_B])
    targets = await router.route("aeron", _spec())

    assert len(targets) == 1
    assert targets[0].marketplace == "alpha"


@pytest.mark.asyncio
async def test_dedupes_duplicate_marketplace_keys():
    llm = _MockLLM([json.dumps({"marketplaces": ["alpha", "alpha", "beta"]})])
    router = MarketplaceRouter(llm=llm, marketplaces=[_M_A, _M_B])
    targets = await router.route("aeron", _spec())

    keys = [t.marketplace for t in targets]
    assert keys == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_all_curated():
    router = MarketplaceRouter(llm=_RaisingLLM(), marketplaces=[_M_A, _M_B])
    targets = await router.route("aeron", _spec())

    assert {t.marketplace for t in targets} == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_malformed_json_falls_back_to_all_curated():
    router = MarketplaceRouter(
        llm=_MockLLM(["not json"]), marketplaces=[_M_A, _M_B],
    )
    targets = await router.route("aeron", _spec())

    assert {t.marketplace for t in targets} == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_empty_llm_selection_falls_back_to_all_curated():
    llm = _MockLLM([json.dumps({"marketplaces": []})])
    router = MarketplaceRouter(llm=llm, marketplaces=[_M_A, _M_B])
    targets = await router.route("aeron", _spec())

    assert {t.marketplace for t in targets} == {"alpha", "beta"}


# ---------------------------------------------------------------------
# Tests — curated registry sanity
# ---------------------------------------------------------------------

def test_curated_marketplaces_have_unique_keys():
    keys = [m.key for m in CURATED_MARKETPLACES]
    assert len(keys) == len(set(keys)), f"duplicate keys: {keys}"


def test_curated_home_urls_are_https():
    for m in CURATED_MARKETPLACES:
        assert m.home_url.startswith("https://"), (
            f"{m.key}: home_url must be https:// got {m.home_url!r}"
        )
