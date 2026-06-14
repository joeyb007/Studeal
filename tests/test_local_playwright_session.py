"""Smoke test for LocalPlaywrightSession.

Verifies the local impl can open a headless Chromium, navigate to a data:
URL with controlled HTML, and that the page is accessible to a perception
snapshot. This is the eval-path session — no Browserbase, no proxies, no
remote API calls. Faster than a Browserbase round-trip and free.

Skipped if Playwright's Chromium isn't installed in the venv. Run
`playwright install chromium` once to enable.
"""

from __future__ import annotations

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
