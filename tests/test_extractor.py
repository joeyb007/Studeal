"""Tests for Extractor — snapshot-to-Offers worker.

Contract exercised:
    - Single LLM call per invocation, no browser interaction.
    - Fresh message list per call (no accumulation).
    - `marketplace` stamped from caller; LLM's own value ignored.
    - Malformed offers (empty url, non-positive price) dropped.
    - LLM failure / malformed JSON → returns [], never raises.
"""

from __future__ import annotations

import json
import re

import pytest

from dealbot.agents.perception import PageSnapshot
from dealbot.agents.workers.extractor import Extractor, Offer
from dealbot.schemas import WatchlistContext


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

class _MockResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _MockLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    async def complete(self, messages, response_format=None, **kwargs):
        self.calls.append([dict(m) for m in messages])
        if self._responses:
            return _MockResponse(self._responses.pop(0))
        return _MockResponse('{"offers":[]}')


class _RaisingLLM:
    async def complete(self, messages, response_format=None, **kwargs):
        raise RuntimeError("simulated LLM outage")


def _snap(url: str = "https://kijiji.ca/b-buy?q=aeron", text: str = "listings...") -> PageSnapshot:
    return PageSnapshot(
        text=text,
        element_map={},
        url=url,
        title="SERP",
        char_count=len(text),
    )


def _spec() -> WatchlistContext:
    return WatchlistContext(product_query="Herman Miller Aeron", max_budget=700.0)


def _offers_json(offers: list[dict]) -> str:
    return json.dumps({"offers": offers})


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_returns_parsed_offers():
    llm = _MockLLM([_offers_json([
        {"title": "Aeron Size B", "price": 450.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/aeron-1"},
        {"title": "Aeron Excellent", "price": 525.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/aeron-2"},
    ])])
    extractor = Extractor(llm=llm)
    offers = await extractor.extract_from_snapshot(_snap(), "kijiji", _spec())

    assert len(offers) == 2
    assert all(isinstance(o, Offer) for o in offers)
    assert {o.title for o in offers} == {"Aeron Size B", "Aeron Excellent"}
    assert all(o.marketplace == "kijiji" for o in offers)


@pytest.mark.asyncio
async def test_marketplace_stamped_from_caller_not_llm():
    """LLM emits a bogus marketplace; caller's value wins."""
    llm = _MockLLM([_offers_json([
        {"title": "X", "price": 100.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/x", "marketplace": "hallucinated"},
    ])])
    extractor = Extractor(llm=llm)
    offers = await extractor.extract_from_snapshot(_snap(), "kijiji", _spec())

    assert len(offers) == 1
    assert offers[0].marketplace == "kijiji"


@pytest.mark.asyncio
async def test_filters_invalid_offers():
    llm = _MockLLM([_offers_json([
        {"title": "Valid", "price": 500.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/valid"},
        {"title": "Zero", "price": 0.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/zero"},
        {"title": "Empty URL", "price": 400.0, "currency": "CAD", "url": ""},
        {"title": "Negative", "price": -1.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/neg"},
    ])])
    extractor = Extractor(llm=llm)
    offers = await extractor.extract_from_snapshot(_snap(), "kijiji", _spec())

    assert len(offers) == 1
    assert offers[0].title == "Valid"


@pytest.mark.asyncio
async def test_llm_failure_returns_empty_no_raise():
    extractor = Extractor(llm=_RaisingLLM())
    offers = await extractor.extract_from_snapshot(_snap(), "kijiji", _spec())
    assert offers == []


@pytest.mark.asyncio
async def test_malformed_json_returns_empty():
    llm = _MockLLM(["not valid json"])
    extractor = Extractor(llm=llm)
    offers = await extractor.extract_from_snapshot(_snap(), "kijiji", _spec())
    assert offers == []


@pytest.mark.asyncio
async def test_fresh_context_across_invocations():
    """Two extract calls → second call's messages contain no content from first."""
    llm = _MockLLM([
        _offers_json([{"title": "First Run", "price": 100.0, "currency": "CAD",
                       "url": "https://kijiji.ca/l/first"}]),
        _offers_json([{"title": "Second Run", "price": 200.0, "currency": "CAD",
                       "url": "https://kijiji.ca/l/second"}]),
    ])
    extractor = Extractor(llm=llm)
    # Distinct pages: an identical snapshot of the same URL is deliberately
    # skipped by the re-extraction memo, which is a different property.
    first = await extractor.extract_from_snapshot(
        _snap(url="https://kijiji.ca/p1", text="page one listings"), "kijiji", _spec())
    second = await extractor.extract_from_snapshot(
        _snap(url="https://kijiji.ca/p2", text="page two listings"), "kijiji", _spec())

    assert first[0].title == "First Run"
    assert second[0].title == "Second Run"

    first_msg_bodies = " ".join(str(m.get("content", "")) for m in llm.calls[0])
    second_msg_bodies = " ".join(str(m.get("content", "")) for m in llm.calls[1])
    assert "First Run" not in second_msg_bodies
    assert "Second Run" not in first_msg_bodies


@pytest.mark.asyncio
async def test_relative_offer_urls_resolved_against_page():
    """Relative FB Marketplace URLs are resolved against the snapshot page URL."""
    fb_snap = _snap(url="https://www.facebook.com/marketplace/toronto/search?query=aeron")
    llm = _MockLLM([_offers_json([
        {"title": "Aeron Chair", "price": 400.0, "currency": "CAD",
         "url": "/marketplace/item/123/?ref=search"},
    ])])
    extractor = Extractor(llm=llm)
    offers = await extractor.extract_from_snapshot(fb_snap, "facebook", _spec())

    assert len(offers) == 1
    assert offers[0].url == "https://www.facebook.com/marketplace/item/123/?ref=search"


@pytest.mark.asyncio
async def test_absolute_offer_urls_pass_through_unchanged():
    """Offers with already-absolute URLs are not modified by urljoin."""
    llm = _MockLLM([_offers_json([
        {"title": "Aeron Size B", "price": 450.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/aeron-specific-listing"},
    ])])
    extractor = Extractor(llm=llm)
    offers = await extractor.extract_from_snapshot(_snap(), "kijiji", _spec())

    assert len(offers) == 1
    assert offers[0].url == "https://kijiji.ca/l/aeron-specific-listing"


# ---------------------------------------------------------------------
# A4 hardening: tolerant row normalization + href clipping
# ---------------------------------------------------------------------

from dealbot.agents.workers.extractor import _clip_hrefs, _parse_and_filter as _paf


def _row_json(**over):
    import json as _json
    row = {"title": "Sony WH-1000XM5", "price": 153.99, "currency": "CAD",
           "url": "https://www.ebay.ca/itm/1"}
    row.update(over)
    return _json.dumps({"offers": [row]})


def test_price_string_with_currency_prefix_is_coerced():
    offers = _paf(_row_json(price="C $153.99", currency="CAD"), "ebay", "https://www.ebay.ca/s")
    assert len(offers) == 1
    assert offers[0].price == 153.99
    assert offers[0].currency == "CAD"


def test_price_string_infers_currency_when_missing():
    offers = _paf(_row_json(price="C $1,234.50", currency="USD"), "ebay", "https://www.ebay.ca/s")
    assert len(offers) == 1
    assert offers[0].price == 1234.50


def test_condition_synonyms_normalize():
    for raw, want in [("Pre-Owned", "used"), ("open box", "used"),
                      ("Certified Refurbished", "refurbished"), ("Brand New", "new"),
                      ("parts only", "unknown")]:
        offers = _paf(_row_json(condition=raw), "ebay", "https://www.ebay.ca/s")
        assert len(offers) == 1, raw
        assert offers[0].condition == want, raw


def test_unparseable_price_still_dropped():
    assert _paf(_row_json(price="contact seller"), "ebay", "https://x.ca") == []


def test_clip_hrefs_shortens_only_long_urls():
    text = '[1]<a href="https://e.ca/itm/1?' + "x" * 500 + '" /> "A" [2]<a href="https://e.ca/b" /> "B"'
    out = _clip_hrefs(text)
    assert len(out) < len(text)
    assert 'href="https://e.ca/b"' in out
    assert "…" in out


# ---------------------------------------------------------------------
# A6: chunked extraction — the extractor must cover the WHOLE snapshot,
# not one window of it. Overlap duplicates are collapsed here so the pool
# and persistence see clean output.
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_large_snapshot_fans_out_over_chunks():
    """A page far past one chunk budget triggers multiple LLM calls, and
    offers from every chunk survive."""
    calls: list[str] = []

    class _ChunkAwareLLM:
        supports_vision = False

        async def complete(self, messages, response_format=None, **kw):
            user = messages[-1]["content"]
            calls.append(user)
            # Distinct offer per chunk: use the HIGHEST card id this chunk
            # saw, so chunks covering different page regions differ. (Every
            # chunk carries the page head by design, so low ids are shared.)
            ids = [int(m) for m in re.findall(r"item/(\d+)", user)]
            marker = max(ids) if ids else -1
            return _MockResponse(json.dumps({"offers": [{
                "title": f"Aeron {marker}", "price": 400.0, "currency": "CAD",
                "url": f"https://m.test/item/{marker}",
            }]}))

    big = "<#document /> \"SERP\"\n" + "".join(
        f'[{i}]<a href="https://m.test/item/{i}" /> "Aeron #{i}"\n\t<#text /> "C ${400+i}.00"\n'
        for i in range(900)
    )
    snap = PageSnapshot(url="https://m.test/s", title="SERP", text=big,
                        char_count=len(big), element_map={})

    offers = await Extractor(_ChunkAwareLLM()).extract_from_snapshot(
        snap, "mock", WatchlistContext(product_query="aeron"),
    )

    assert len(calls) > 1, "large page did not fan out into chunks"
    assert len(offers) >= 2, f"offers from later chunks lost: {offers}"


@pytest.mark.asyncio
async def test_duplicate_offers_across_chunks_collapse():
    """Overlap means the same card can appear twice — dedupe on url."""
    class _DupLLM:
        supports_vision = False

        async def complete(self, messages, response_format=None, **kw):
            return _MockResponse(json.dumps({"offers": [{
                "title": "Aeron", "price": 400.0, "currency": "CAD",
                "url": "https://m.test/item/same",
            }]}))

    big = "<#document /> \"SERP\"\n" + ("[1]<a href=\"https://m.test/x\" /> \"card\"\n" * 3000)
    snap = PageSnapshot(url="https://m.test/s", title="SERP", text=big,
                        char_count=len(big), element_map={})

    offers = await Extractor(_DupLLM()).extract_from_snapshot(
        snap, "mock", WatchlistContext(product_query="aeron"),
    )
    assert len(offers) == 1, f"duplicates not collapsed: {len(offers)}"


@pytest.mark.asyncio
async def test_small_snapshot_still_single_call():
    """No fan-out tax on ordinary pages."""
    calls = []

    class _CountingLLM:
        supports_vision = False

        async def complete(self, messages, response_format=None, **kw):
            calls.append(1)
            return _MockResponse(json.dumps({"offers": []}))

    snap = PageSnapshot(url="https://m.test/s", title="SERP", text="small page",
                        char_count=10, element_map={})
    await Extractor(_CountingLLM()).extract_from_snapshot(
        snap, "mock", WatchlistContext(product_query="aeron"),
    )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_extract_joins_snapshot_image_map():
    """Captured thumbnail wins over the LLM-emitted image_url; join is by
    canonical URL (tracking params stripped)."""
    llm = _MockLLM([_offers_json([
        {"title": "Aeron chair", "price": 500.0, "currency": "CAD",
         "url": "https://www.ebay.ca/itm/123?hash=x",
         "image_url": "https://hallucinated.example/x.jpg"},
    ])])
    snap = _snap(url="https://www.ebay.ca/sch/i.html?_nkw=aeron")
    snap.image_map = {
        "https://www.ebay.ca/itm/123": "https://i.ebayimg.com/images/g/a/s-l500.webp",
    }
    extractor = Extractor(llm=llm)
    offers = await extractor.extract_from_snapshot(snap, "ebay", _spec())

    assert len(offers) == 1
    assert offers[0].image_url == "https://i.ebayimg.com/images/g/a/s-l500.webp"


@pytest.mark.asyncio
async def test_extract_drops_llm_image_url_on_join_miss():
    """Empty image_map: the hallucinated LLM value is cleared, not kept."""
    llm = _MockLLM([_offers_json([
        {"title": "Aeron chair", "price": 500.0, "currency": "CAD",
         "url": "https://www.ebay.ca/itm/123",
         "image_url": "https://hallucinated.example/x.jpg"},
    ])])
    extractor = Extractor(llm=llm)
    offers = await extractor.extract_from_snapshot(
        _snap(url="https://www.ebay.ca/sch/i.html?_nkw=aeron"), "ebay", _spec(),
    )

    assert len(offers) == 1
    assert offers[0].image_url is None


def test_clip_hrefs_keeps_realistic_listing_urls_intact():
    """Kijiji listing paths run to ~150 chars; clipping them mid-slug made the
    LLM invent URL completions (fabricated ad IDs observed 2026-08-03)."""
    url = "https://www.kijiji.ca/v-chair-recliner/city-of-toronto/" + "a" * 80 + "/1739463548"
    text = f'[1]<a href="{url}" /> "A"'
    assert _clip_hrefs(text) == text


@pytest.mark.asyncio
async def test_ungrounded_offer_urls_dropped():
    """An offer whose URL matches no anchor in the snapshot is fabricated —
    drop it. Grounded offers survive, including relative-href anchors."""
    from dealbot.agents.perception import ElementRef

    llm = _MockLLM([_offers_json([
        {"title": "Real chair", "price": 100.0, "currency": "CAD",
         "url": "https://www.kijiji.ca/v-chair/toronto/real-chair/1739000001"},
        {"title": "Invented chair", "price": 120.0, "currency": "CAD",
         "url": "https://www.kijiji.ca/v-chair/toronto/invented/1700000001"},
    ])])
    snap = _snap(url="https://www.kijiji.ca/b-buy-sell/office-chair/k0c10")
    snap.element_map = {
        1: ElementRef(
            backend_node_id=1, role="link", name="Real chair", tag_name="a",
            value=None, bbox=(0, 0, 100, 20), is_interactive=True,
            attributes={"href": "/v-chair/toronto/real-chair/1739000001?ref=srp"},
        ),
    }
    extractor = Extractor(llm=llm)
    offers = await extractor.extract_from_snapshot(snap, "kijiji", _spec())

    assert [o.title for o in offers] == ["Real chair"]


@pytest.mark.asyncio
async def test_grounding_skipped_when_snapshot_has_no_hrefs():
    """No anchors in the snapshot (fixtures, degenerate pages) is not evidence
    of fabrication — offers pass through."""
    llm = _MockLLM([_offers_json([
        {"title": "Aeron", "price": 450.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/aeron-1"},
    ])])
    extractor = Extractor(llm=llm)
    offers = await extractor.extract_from_snapshot(_snap(), "kijiji", _spec())
    assert len(offers) == 1


# ---------------------------------------------------------------------------
# Discount-decoy guard (2026-08-13): "SAVE $200" must never become the price.
# ---------------------------------------------------------------------------

def test_save_badge_price_is_a_decoy():
    from dealbot.agents.workers.extractor import _is_discount_decoy

    page = 'Open Box AirPods Max SAVE $200 $599.99 Add to cart'
    assert _is_discount_decoy(page, 200.0) is True
    assert _is_discount_decoy(page, 599.99) is False


def test_off_suffix_and_comma_amounts_are_decoys():
    from dealbot.agents.workers.extractor import _is_discount_decoy

    assert _is_discount_decoy("MacBook Pro $1,500 off this week", 1500.0) is True
    assert _is_discount_decoy("save $50.00 on accessories", 50.0) is True


def test_legitimate_prices_survive():
    from dealbot.agents.workers.extractor import _is_discount_decoy

    # A $200 listing on a page with no discount language keeps its price.
    assert _is_discount_decoy("Golf club set $200 Toronto", 200.0) is False
    # Cents-precision prices are never treated as decoys.
    assert _is_discount_decoy("SAVE $199.99 today", 199.99) is False


@pytest.mark.asyncio
async def test_priceless_chunks_are_skipped_when_the_page_has_prices():
    """Extraction is the largest LLM line item (51% of tagged spend). A chunk
    with no price token cannot yield an Offer, so page chrome is not worth a
    call — but only filter where the regex has proved it reads this site."""
    from dealbot.agents.workers import extractor as ex

    seen: list[str] = []

    class _LLM:
        async def complete(self, messages, **kw):
            seen.append(messages[1]["content"])
            return type("R", (), {"content": '{"offers": []}'})()

    # Two chunks' worth of text: one priced, one pure chrome.
    text = ("$19.99 real listing here " * 400) + ("navigation footer help " * 400)
    snap = _snap(text=text)
    await ex.Extractor(_LLM()).extract_from_snapshot(snap, "kijiji", _spec())
    assert seen, "at least the priced chunk must be extracted"
    assert all("$" in c or "C $" in c for c in seen), "chrome-only chunks skipped"


@pytest.mark.asyncio
async def test_no_filtering_when_the_page_shows_no_recognisable_price():
    """Prices as images / unfamiliar layout: the regex knows nothing here, so
    filtering would silently drop real listings. It must filter nothing."""
    from dealbot.agents.workers import extractor as ex

    calls = {"n": 0}

    class _LLM:
        async def complete(self, messages, **kw):
            calls["n"] += 1
            return type("R", (), {"content": '{"offers": []}'})()

    snap = _snap(text="listing card without any parseable price " * 900)
    await ex.Extractor(_LLM()).extract_from_snapshot(snap, "kijiji", _spec())
    assert calls["n"] >= 2, "every chunk still extracted when prices are unreadable"


@pytest.mark.asyncio
async def test_same_page_is_not_re_extracted_across_growth_snapshots():
    """Infinite scroll sinks a new snapshot per growth, each carrying the whole
    page — so prefix chunks repeat verbatim. Re-extracting them is 51%-of-spend
    waste (2026-08-17); the memo skips exactly those repeats."""
    from dealbot.agents.workers import extractor as ex

    calls = {"n": 0}

    class _LLM:
        async def complete(self, messages, **kw):
            calls["n"] += 1
            return type("R", (), {"content": '{"offers": []}'})()

    e = ex.Extractor(_LLM())
    page = "$10.00 card " * 900
    await e.extract_from_snapshot(_snap(url="https://kijiji.ca/feed", text=page), "kijiji", _spec())
    first_round = calls["n"]
    assert first_round >= 1

    # Same URL, page has grown: complete prefix chunks repeat verbatim (the
    # final chunk of the smaller page was truncated, so it legitimately differs
    # and is re-sent). Compare against what a memo-less run would have cost.
    from dealbot.agents.perception import chunk_snapshot_text

    grown = page + "$20.00 newcard " * 900
    grown_chunks = len(chunk_snapshot_text(grown))
    await e.extract_from_snapshot(
        _snap(url="https://kijiji.ca/feed", text=grown), "kijiji", _spec(),
    )
    second_round = calls["n"] - first_round
    assert second_round < grown_chunks, (
        "prefix chunks already extracted must not be re-sent "
        f"(sent {second_round} of {grown_chunks})"
    )

    # A different URL sharing text is still extracted: relative links resolve
    # against the page, so identical text can mean different listings.
    await e.extract_from_snapshot(_snap(url="https://kijiji.ca/other", text=page), "kijiji", _spec())
    assert calls["n"] > first_round
