from __future__ import annotations

import logging
import os
from urllib.parse import quote_plus

import httpx
import trafilatura
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

_BROWSERBASE_API = "https://api.browserbase.com/v1"
_PAGE_TIMEOUT = 20_000  # ms


async def search_shopping(query: str) -> str:
    """Navigate to Google Shopping and return trafilatura-extracted page text."""
    url = f"https://www.google.com/search?q={quote_plus(query)}&tbm=shop&gl=us&hl=en"
    return await _fetch_via_browserbase(url)


async def fetch_page(url: str) -> str:
    """Fetch any URL via Browserbase and return trafilatura-extracted page text."""
    return await _fetch_via_browserbase(url)


async def _fetch_via_browserbase(url: str) -> str:
    api_key = os.environ.get("BROWSERBASE_API_KEY", "")
    project_id = os.environ.get("BROWSERBASE_PROJECT_ID", "")

    if not api_key or not project_id:
        logger.warning("browser_agent: BROWSERBASE_API_KEY or BROWSERBASE_PROJECT_ID not set")
        return ""

    try:
        connect_url = await _create_session(api_key, project_id)
    except Exception as exc:
        logger.warning("browser_agent: failed to create Browserbase session: %s", exc)
        return ""

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(connect_url)
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=_PAGE_TIMEOUT)
            html = await page.content()
            await browser.close()

        text = trafilatura.extract(
            html, include_links=True, output_format="txt", no_fallback=False
        )
        return text or ""
    except Exception as exc:
        logger.warning("browser_agent: failed to fetch %s: %s", url, exc)
        return ""


async def _create_session(api_key: str, project_id: str) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_BROWSERBASE_API}/sessions",
            headers={"X-BB-API-Key": api_key, "Content-Type": "application/json"},
            json={"projectId": project_id},
        )
        resp.raise_for_status()
    return resp.json()["connectUrl"]
