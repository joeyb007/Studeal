"""Smoke test for LocalPlaywrightSession.

Verifies the local impl can open a headless Chromium, navigate to a data:
URL with controlled HTML, and that the page is accessible to a perception
snapshot. This is the eval-path session — no Browserbase, no proxies, no
remote API calls. Faster than a Browserbase round-trip and free.

Skipped if Playwright's Chromium isn't installed in the venv. Run
`playwright install chromium` once to enable.
"""

from __future__ import annotations

import asyncio

import pytest

from dealbot.agents.perception import snapshot_page
from dealbot.scrapers.browser_session import (
    BrowserSession,
    LocalPlaywrightSession,
    build_browser_session,
)


_SAMPLE_HTML = """
<!doctype html>
<html>
  <head><title>LPS Smoke</title></head>
  <body>
    <h1>Sample Page</h1>
    <input type="search" placeholder="Search products..." />
    <button>Go</button>
    <a href="/next">Next page</a>
  </body>
</html>
"""


def _is_playwright_browser_installed() -> bool:
    """Cheap probe: Chromium binary present where Playwright expects it."""
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        return False
    import os
    cache = os.path.expanduser("~/Library/Caches/ms-playwright")
    alt = os.path.expanduser("~/.cache/ms-playwright")
    return os.path.isdir(cache) or os.path.isdir(alt)


pytestmark = pytest.mark.skipif(
    not _is_playwright_browser_installed(),
    reason="Playwright Chromium not installed (run `playwright install chromium`).",
)


@pytest.mark.asyncio
async def test_local_session_opens_and_navigates():
    """The session yields a live Page; we can navigate it; cleanup runs."""
    async with LocalPlaywrightSession() as bs:
        assert bs.page is not None
        assert bs.watchdog is not None
        assert bs.intercepted_responses == []

        # data: URL renders without a network round-trip
        await bs.page.set_content(_SAMPLE_HTML)
        title = await bs.page.title()
        assert title == "LPS Smoke"


@pytest.mark.asyncio
async def test_local_session_works_with_snapshot_page():
    """Perception works on a real Page from LocalPlaywrightSession."""
    async with LocalPlaywrightSession() as bs:
        await bs.page.set_content(_SAMPLE_HTML)
        snap = await snapshot_page(bs.page)

        # We expect button + input + link to surface as interactive
        interactive_count = sum(
            1 for e in snap.element_map.values() if e.is_interactive
        )
        assert interactive_count >= 3, (
            f"expected ≥3 interactive elements, got {interactive_count}; "
            f"text={snap.text!r}"
        )
        # Heading text or link text should appear in serialized output
        assert "Sample Page" in snap.text or "Next page" in snap.text


@pytest.mark.asyncio
async def test_build_browser_session_local_backend(monkeypatch):
    """Composition root returns a LocalPlaywrightSession when env is 'local'."""
    monkeypatch.setenv("AGENT_BROWSER_BACKEND", "local")
    sess = build_browser_session()
    assert isinstance(sess, LocalPlaywrightSession)
    assert isinstance(sess, BrowserSession)   # ABC contract honored


@pytest.mark.asyncio
async def test_build_browser_session_explicit_arg_wins(monkeypatch):
    """Explicit arg overrides AGENT_BROWSER_BACKEND env var."""
    monkeypatch.setenv("AGENT_BROWSER_BACKEND", "browserbase")
    sess = build_browser_session(backend="local")
    assert isinstance(sess, LocalPlaywrightSession)


def test_build_browser_session_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("AGENT_BROWSER_BACKEND", "weasel")
    with pytest.raises(ValueError, match="Unknown AGENT_BROWSER_BACKEND"):
        build_browser_session()


# ---------------------------------------------------------------------
# Popup / new-tab handling (v14 spec §9)
#
# Contract: sites spawn tabs via target="_blank" or window.open(). Handler
# either promotes the new tab to `self.page` (real URL) or closes it (blank).
# Without the handler, popups orphan and the session freezes.
# ---------------------------------------------------------------------

async def _wait_for(
    predicate, *, timeout_s: float = 3.0, poll_s: float = 0.05,
) -> None:
    """Poll until predicate returns True or timeout. Raises on timeout."""
    async def _spin():
        while not predicate():
            await asyncio.sleep(poll_s)
    await asyncio.wait_for(_spin(), timeout=timeout_s)


@pytest.mark.asyncio
async def test_popup_with_real_url_promotes_to_self_page():
    """window.open with a real URL → self.page swaps to the new tab; old closes."""
    async with LocalPlaywrightSession() as bs:
        await bs.page.set_content(_SAMPLE_HTML)
        original_page = bs.page

        # Trigger a popup with a real (data:) URL. Playwright's `page` event
        # fires on the BrowserContext; our handler must promote it.
        await bs.page.evaluate(
            "() => window.open('data:text/html,<h1>Popup Page</h1>', '_blank')"
        )

        # Wait for the handler to swap self.page
        await _wait_for(lambda: bs.page is not original_page)

        assert bs.page is not original_page
        assert "Popup Page" in await bs.page.content()
        # Old page should be closed as part of promotion.
        assert original_page.is_closed(), (
            "handler must close the old page after promoting the new tab"
        )


@pytest.mark.asyncio
async def test_popup_with_blank_url_is_closed_no_promotion():
    """window.open('about:blank') → new tab closed; self.page unchanged."""
    async with LocalPlaywrightSession() as bs:
        await bs.page.set_content(_SAMPLE_HTML)
        original_page = bs.page

        # about:blank popup — pure noise, should be discarded
        await bs.page.evaluate(
            "() => window.open('about:blank', '_blank')"
        )

        # Give the handler time to fire and close the popup
        await asyncio.sleep(0.5)

        assert bs.page is original_page, "self.page must not change for blank popup"
        assert not original_page.is_closed(), "original must remain open"
        # Context should have exactly one page — the original
        ctx_pages = original_page.context.pages
        assert len(ctx_pages) == 1, (
            f"expected 1 page after blank popup closed, got {len(ctx_pages)}"
        )


@pytest.mark.asyncio
async def test_popup_promotion_swaps_watchdog():
    """After promotion, `self.watchdog` targets the new page (not the old)."""
    async with LocalPlaywrightSession() as bs:
        await bs.page.set_content(_SAMPLE_HTML)
        original_watchdog = bs.watchdog

        await bs.page.evaluate(
            "() => window.open('data:text/html,<h1>New</h1>', '_blank')"
        )
        await _wait_for(lambda: bs.watchdog is not original_watchdog)

        assert bs.watchdog is not original_watchdog, (
            "handler must rebuild the watchdog for the promoted page"
        )
        assert bs.watchdog._page is bs.page, (  # type: ignore[attr-defined]
            "new watchdog must be bound to the current self.page"
        )
