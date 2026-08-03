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
    build_search_url=lambda q: f"https://alpha.test/search?q={q}",
)
_M_B = MarketplaceConfig(
    key="beta", display_name="Beta", description="fake beta",
    build_search_url=lambda q: f"https://beta.test/search?q={q}",
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
    assert targets[0].entry_url == "https://alpha.test/search?q=aeron chair"


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


def test_curated_search_urls_are_https_and_contain_query():
    for m in CURATED_MARKETPLACES:
        url = m.build_search_url("herman miller aeron")
        assert url.startswith("https://"), f"{m.key}: search URL must be https:// got {url!r}"
        # Browse-only stores (e.g. Apple Refurbished) have no search URL —
        # detectable as query-insensitive templates; the agent browses from
        # the entry page instead.
        browse_only = url == m.build_search_url("different query")
        if not browse_only:
            assert "herman" in url.lower() or "aeron" in url.lower(), (
                f"{m.key}: search URL should contain query terms, got {url!r}"
            )


def test_fb_target_carries_google_referer():
    """FB serves its public page to search referrals; targets must carry it."""
    from dealbot.agents.marketplace_router import CURATED_MARKETPLACES
    fb = next(m for m in CURATED_MARKETPLACES if m.key == "fb_marketplace")
    assert fb.entry_referer == "https://www.google.com/"


def test_targets_propagate_entry_referer():
    from dealbot.agents.marketplace_router import (
        CURATED_MARKETPLACES, MarketplaceRouter,
    )
    router = MarketplaceRouter(llm=None)
    targets = router._all_targets("aeron chair")
    by_key = {t.marketplace: t for t in targets}
    assert by_key["fb_marketplace"].entry_referer == "https://www.google.com/"
    assert by_key["kijiji"].entry_referer is None


def test_capture_config_present_for_probed_marketplaces():
    from dealbot.agents.marketplace_router import CONFIG_BY_KEY

    expected = {
        "kijiji": ("/v-", ("media.kijiji.ca",)),
        "fb_marketplace": ("/marketplace/item/", (".fbcdn.net",)),
        "ebay": ("/itm/", ("i.ebayimg.com",)),
        "craigslist": ("/d/", ("images.craigslist.org",)),
    }
    for key, (pattern, hosts) in expected.items():
        cfg = CONFIG_BY_KEY[key]
        assert cfg.listing_href_pattern == pattern
        assert cfg.image_cdn_hosts == hosts


def test_unprobed_marketplaces_have_no_capture_pattern():
    from dealbot.agents.marketplace_router import CONFIG_BY_KEY

    for key, cfg in CONFIG_BY_KEY.items():
        if key not in {"kijiji", "fb_marketplace", "ebay", "craigslist"}:
            assert cfg.listing_href_pattern is None
            assert cfg.image_cdn_hosts == ()
