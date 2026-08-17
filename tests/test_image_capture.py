"""Unit tests for deterministic thumbnail capture helpers (no browser)."""

from __future__ import annotations

import os

import pytest

from dealbot.agents.image_capture import attach_images, pick_image_url, spec_for

HOSTS = ("i.ebayimg.com",)


def _cand(current_src="", src_attr="", natural_width=0):
    return {"currentSrc": current_src, "srcAttr": src_attr, "naturalWidth": natural_width}


def test_loaded_current_src_wins():
    c = _cand(current_src="https://i.ebayimg.com/images/g/abc/s-l500.webp", natural_width=500)
    assert pick_image_url(c, HOSTS) == "https://i.ebayimg.com/images/g/abc/s-l500.webp"


def test_unloaded_falls_back_to_src_attr():
    c = _cand(src_attr="https://i.ebayimg.com/images/g/abc/s-l500.webp", natural_width=0)
    assert pick_image_url(c, HOSTS) == "https://i.ebayimg.com/images/g/abc/s-l500.webp"


def test_off_whitelist_host_rejected():
    # eBay's static placeholder graphic lives on ir.ebaystatic.com, not the
    # product-image CDN. Probe finding: below-fold lazy cards carry it in src.
    c = _cand(src_attr="https://ir.ebaystatic.com/rs/v/fxxj.png", natural_width=200)
    assert pick_image_url(c, HOSTS) is None


def test_data_uri_and_relative_rejected():
    assert pick_image_url(_cand(src_attr="data:image/svg+xml;base64,xx"), HOSTS) is None
    assert pick_image_url(_cand(src_attr="/img/spinner.gif"), HOSTS) is None


def test_small_image_ignored_for_current_src():
    # naturalWidth < 64 means icon/avatar/placeholder pixel: currentSrc is not
    # trusted, but a whitelisted src attr may still be used.
    c = _cand(
        current_src="https://i.ebayimg.com/images/g/icon/s-l64.webp",
        src_attr="https://i.ebayimg.com/images/g/real/s-l500.webp",
        natural_width=16,
    )
    assert pick_image_url(c, HOSTS) == "https://i.ebayimg.com/images/g/real/s-l500.webp"


def test_fbcdn_suffix_match():
    hosts = (".fbcdn.net",)
    c = _cand(current_src="https://scontent.fyto1-1.fna.fbcdn.net/v/t39/762.jpg", natural_width=526)
    assert pick_image_url(c, hosts) == "https://scontent.fyto1-1.fna.fbcdn.net/v/t39/762.jpg"


def test_spec_for_known_and_unknown():
    assert spec_for("kijiji") is not None
    assert spec_for("bestbuy_outlet") is not None    # probed 2026-08-13
    assert spec_for("canada_computers") is None
    assert spec_for("nonexistent") is None


def test_attach_images_joins_by_canonical_url():
    from dealbot.agents.workers.extractor import Offer

    offers = [
        Offer(title="A", price=10.0, url="https://www.ebay.ca/itm/123?hash=xyz", marketplace="ebay"),
        Offer(title="B", price=20.0, url="https://www.ebay.ca/itm/456", marketplace="ebay",
              image_url="https://hallucinated.example/x.jpg"),
    ]
    image_map = {"https://www.ebay.ca/itm/123": "https://i.ebayimg.com/images/g/abc/s-l500.webp"}
    attach_images(offers, image_map, "ebay")
    # Tracking params stripped by canonicalization: join hits.
    assert offers[0].image_url == "https://i.ebayimg.com/images/g/abc/s-l500.webp"
    # Deterministic-only invariant: LLM-emitted URL is dropped, not kept.
    assert offers[1].image_url is None


# ---------------------------------------------------------------------------
# Integration: real page (pattern from tests/test_dom_settlement.py)
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


_FIXTURE_HTML = """
<html><body>
  <ul>
    <li>
      <img src="https://media.kijiji.ca/api/v1/x/images/aa?rule=kijijica-400-webp">
      <a href="https://www.kijiji.ca/v-chair/toronto/aeron/1001?utm_source=share">Aeron chair</a>
    </li>
    <li>
      <img src="data:image/svg+xml;base64,PHN2Zy8+">
      <a href="https://www.kijiji.ca/v-desk/toronto/desk/1002">Standing desk</a>
    </li>
    <li>
      <img src="https://ads.example.net/banner.jpg">
      <a href="https://www.kijiji.ca/v-lamp/toronto/lamp/1003">Lamp</a>
    </li>
  </ul>
</body></html>
"""


@pytest.mark.skipif(
    not _playwright_browser_installed(),
    reason="Playwright Chromium not installed.",
)
@pytest.mark.asyncio
async def test_capture_against_fixture_page():
    from dealbot.agents.image_capture import ImageCaptureSpec, capture_card_images
    from dealbot.scrapers.browser_session import LocalPlaywrightSession

    spec = ImageCaptureSpec(href_pattern="/v-", cdn_hosts=("media.kijiji.ca",))
    async with LocalPlaywrightSession() as bs:
        await bs.page.set_content(_FIXTURE_HTML)
        image_map = await capture_card_images(bs.page, spec, "kijiji")

    # Card 1: real CDN URL, keyed by canonical (tracking params stripped).
    # Card 2 (data URI) and card 3 (off-whitelist ad host) produce NO entry:
    # the safety invariant is a missing image, never a wrong one.
    assert image_map == {
        "https://www.kijiji.ca/v-chair/toronto/aeron/1001":
            "https://media.kijiji.ca/api/v1/x/images/aa?rule=kijijica-400-webp"
    }


def test_srcset_fallback_when_src_empty():
    # openbox probe 2026-08-15: product img has empty src, URL only in srcset.
    from dealbot.agents.image_capture import pick_image_url
    cand = {"currentSrc": "", "srcAttr": "", "naturalWidth": 0,
            "srcset": "//openbox.ca/cdn/shop/files/x_200x.jpg 200w, //openbox.ca/cdn/shop/files/x_400x.jpg 400w"}
    assert pick_image_url(cand, ("openbox.ca",)) == "https://openbox.ca/cdn/shop/files/x_200x.jpg"


def test_largest_area_beats_badge():
    # The 54px 'refurbished' badge has a src; the 300px product shot only a
    # srcset. Area ordering must pick the product.
    from dealbot.agents.image_capture import pick_card_image
    badge = {"currentSrc": "", "srcAttr": "https://cdn.shopify.com/s/files/badge-refurbished.png",
             "naturalWidth": 0, "srcset": "", "w": 54, "h": 48}
    product = {"currentSrc": "", "srcAttr": "",
               "naturalWidth": 0, "srcset": "//openbox.ca/cdn/shop/files/prod_200x.jpg 200w", "w": 300, "h": 300}
    hosts = ("cdn.shopify.com", "openbox.ca")
    assert pick_card_image([badge, product], hosts) == "https://openbox.ca/cdn/shop/files/prod_200x.jpg"


def test_pick_card_image_falls_back_in_document_order():
    from dealbot.agents.image_capture import pick_card_image
    a = {"currentSrc": "", "srcAttr": "https://media.kijiji.ca/a.jpg", "naturalWidth": 0, "srcset": "", "w": 0, "h": 0}
    b = {"currentSrc": "", "srcAttr": "https://media.kijiji.ca/b.jpg", "naturalWidth": 0, "srcset": "", "w": 0, "h": 0}
    assert pick_card_image([a, b], ("media.kijiji.ca",)) == "https://media.kijiji.ca/a.jpg"


def test_newegg_spec_present():
    from dealbot.agents.image_capture import spec_for
    spec = spec_for("newegg_ca")
    assert spec is not None and spec.href_pattern == "/p/"
    assert ".neweggimages.com" in spec.cdn_hosts


def test_href_exclude_filters_navigation_links():
    """Some sites reuse the listing prefix for navigation: newegg products are
    /p/<sku> while its related-search links are /p/pl?d=... — every /p/ anchor
    on a newegg SERP was a related search (2026-08-17, 3% image coverage)."""
    spec = spec_for("newegg_ca")
    assert spec is not None
    assert spec.href_pattern == "/p/"
    assert spec.href_exclude == "/p/pl"


def test_marketplaces_without_exclusion_keep_a_plain_selector():
    for key in ("kijiji", "ebay", "craigslist"):
        spec = spec_for(key)
        assert spec is not None
        assert spec.href_exclude is None
