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
