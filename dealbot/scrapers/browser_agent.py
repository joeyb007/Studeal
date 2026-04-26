from __future__ import annotations

import asyncio
import codecs
import logging
import os
from urllib.parse import quote_plus

import httpx
from playwright.async_api import (
    Page,
    async_playwright,
    BrowserContext,
)

from dealbot.llm.base import LLMClient

logger = logging.getLogger(__name__)

_BROWSERBASE_API = "https://api.browserbase.com/v1"
_PAGE_TIMEOUT = 20_000  # ms


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

class BrowserSession:
    """
    Async context manager owning a single Browserbase session.
    Provides a Playwright Page shared across all tool calls in one run().
    """

    def __init__(self) -> None:
        self._api_key = os.environ.get("BROWSERBASE_API_KEY", "")
        self._project_id = os.environ.get("BROWSERBASE_PROJECT_ID", "")
        self._session_id: str | None = None
        self._pw_context = None
        self._browser = None
        self.page: Page | None = None

    async def __aenter__(self) -> BrowserSession:
        if not self._api_key or not self._project_id:
            raise RuntimeError("BROWSERBASE_API_KEY or BROWSERBASE_PROJECT_ID not set")

        self._session_id, connect_url = await _create_session(
            self._api_key, self._project_id,
        )
        self._pw_context = async_playwright()
        pw = await self._pw_context.__aenter__()
        self._browser = await pw.chromium.connect_over_cdp(connect_url)
        ctx: BrowserContext = self._browser.contexts[0]
        self.page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw_context:
            try:
                await self._pw_context.__aexit__(None, None, None)
            except Exception:
                pass
        if self._session_id:
            await _terminate_session(self._api_key, self._session_id)


# ---------------------------------------------------------------------------
# search_shopping — returns structured text for the orchestrator LLM
# ---------------------------------------------------------------------------

class ShoppingResult:
    """Result from search_shopping: filtered text + metadata for find_url."""

    def __init__(self, text: str, query: str, button_labels: list[str]) -> None:
        self.text = text
        self.query = query
        self.button_labels = button_labels


async def search_shopping(page: Page, query: str) -> ShoppingResult:
    """
    Navigate to Google Shopping, capture an aria snapshot (pierces shadow DOM),
    and return structured deal data + button labels for find_url.
    Expects a live Page from BrowserSession — does NOT manage sessions.
    """
    shop_url = f"https://www.google.com/search?q={quote_plus(query)}&tbm=shop&gl=ca&hl=en-CA"
    try:
        await page.goto(shop_url, wait_until="domcontentloaded", timeout=_PAGE_TIMEOUT)
        snap = await page.locator("body").aria_snapshot()
        logger.debug("browser_agent: aria_snapshot %d chars for %r", len(snap), query)

        if len(snap) < 500:
            logger.warning("browser_agent: short aria_snapshot — possible CAPTCHA")
            return ShoppingResult("", query, [])

        text = _filter_aria_snapshot(snap)
        button_labels = _extract_button_labels(snap)
        logger.debug("browser_agent: compiled %d chars, %d buttons for %r",
                      len(text), len(button_labels), query)
        return ShoppingResult(text, query, button_labels)
    except Exception as exc:
        logger.warning("browser_agent: search_shopping failed for %r: %s", query, exc)
        return ShoppingResult("", query, [])


async def fetch_page(url: str) -> str:
    """Fetch any URL via a fresh Browserbase session and return rendered page text."""
    api_key = os.environ.get("BROWSERBASE_API_KEY", "")
    project_id = os.environ.get("BROWSERBASE_PROJECT_ID", "")
    if not api_key or not project_id:
        return ""

    session_id = None
    text = ""
    try:
        session_id, connect_url = await _create_session(api_key, project_id)
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(connect_url)
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=_PAGE_TIMEOUT)
            text = await page.inner_text("body")
            await browser.close()
    except Exception as exc:
        logger.warning("browser_agent: fetch_page failed for %s: %s", url, exc)
    finally:
        if session_id:
            await _terminate_session(api_key, session_id)
    return text


# ---------------------------------------------------------------------------
# find_url — hybrid deterministic + LLM fallback URL resolver
# ---------------------------------------------------------------------------

_PICK_URL_PROMPT = """\
Given this product and merchant, pick the single best matching URL from the list.

Product: {title}
Merchant: {merchant}

Available URLs:
{url_list}

Rules:
- Pick the URL whose merchant name best matches "{merchant}"
- The URL should be for the specific product, not a different model
- Return ONLY the URL as a plain string — nothing else
- If no URL is a reasonable match, return the string "none"
"""


async def find_url(
    llm: LLMClient,
    title: str,
    merchant: str,
    button_label: str,
    search_query: str,
) -> str:
    """
    Resolve the direct retailer URL for an organic listing.

    Opens a fresh Browserbase session with proxies for each call so Google
    sees each resolution attempt as a distinct user — prevents per-session
    throttling that occurs when reusing a shared session across many clicks.

    Strategy (deterministic first, LLM fallback second):
    1. Open fresh session + proxy, navigate to search results page
    2. Click the exact product button (label stored from search_shopping)
    3. Aria snapshot the detail panel
    4. Parse all merchant → URL pairs — deterministic
    5. Fuzzy-match the target merchant → return URL
    6. If no match: single LLM call to pick the best URL from the list

    Returns the URL string, or "" if not found.
    """
    api_key = os.environ.get("BROWSERBASE_API_KEY", "")
    project_id = os.environ.get("BROWSERBASE_PROJECT_ID", "")
    if not api_key or not project_id:
        logger.warning("find_url: BROWSERBASE credentials not set")
        return ""

    session_id = None
    snap = ""
    fail_step = ""
    try:
        session_id, connect_url = await _create_session(api_key, project_id, proxies=True)
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(connect_url)
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            # Step 1: Navigate to the search results page
            shop_url = f"https://www.google.com/search?q={quote_plus(search_query)}&tbm=shop&gl=ca&hl=en-CA"
            try:
                await page.goto(shop_url, wait_until="domcontentloaded", timeout=_PAGE_TIMEOUT)
            except Exception as exc:
                fail_step = "navigate"
                logger.warning("find_url [%s]: %s for %r", fail_step, exc, title[:40])
                await browser.close()
                return ""

            # Step 2: Click the exact product button
            clean_label = codecs.decode(button_label, "unicode_escape")
            btn = page.get_by_role("button", name=clean_label)
            count = await btn.count()
            if count == 0:
                btn = page.get_by_role("button", name=clean_label[:60], exact=False)
                count = await btn.count()
            if count == 0:
                fail_step = "button_not_found"
                logger.warning("find_url [%s]: %r (label=%r)", fail_step, title[:40], button_label[:60])
                await browser.close()
                return ""

            try:
                await btn.first.click(timeout=10_000)
                await page.wait_for_timeout(2_500)
            except Exception as exc:
                fail_step = "click"
                logger.warning("find_url [%s]: %s for %r", fail_step, exc, title[:40])
                await browser.close()
                return ""

            # Step 3: Aria snapshot of the detail panel
            try:
                snap = await page.locator("body").aria_snapshot()
            except Exception as exc:
                fail_step = "snapshot"
                logger.warning("find_url [%s]: %s for %r", fail_step, exc, title[:40])
                await browser.close()
                return ""

            await browser.close()
    except Exception as exc:
        fail_step = "session"
        logger.warning("find_url [%s]: %s for %r", fail_step, exc, title[:40])
        return ""
    finally:
        if session_id:
            await _terminate_session(api_key, session_id)

    if not snap:
        fail_step = "empty_snap"
        logger.warning("find_url [%s]: %r", fail_step, title[:40])
        return ""

    # Step 4: Parse merchant → URL pairs from the detail panel
    panel_indicators = ("Current price", "Compare prices", "Visit site", "Buy now", "See offer")
    panel_opened = any(ind in snap for ind in panel_indicators)
    logger.debug(
        "find_url: post-click snapshot %d chars, panel_opened=%s, title=%r",
        len(snap), panel_opened, title[:40],
    )
    if not panel_opened:
        logger.debug("find_url: panel did not open — snapshot fragment: %s", snap[:300])

    merchant_urls = _extract_merchant_urls(snap)
    if not merchant_urls:
        logger.debug(
            "find_url: no merchant URLs parsed from panel for %r (panel_opened=%s)",
            title[:40], panel_opened,
        )
        _dump_snap(title, merchant, snap, reason="no_merchant_urls")
        return ""

    # Step 5: Deterministic fuzzy match
    merchant_lower = merchant.lower()
    for m_name, m_url in merchant_urls:
        if m_name.lower() in merchant_lower or merchant_lower in m_name.lower():
            logger.debug("find_url: deterministic match %r → %s", title[:40], m_url[:80])
            return m_url

    # Step 6: LLM fallback — single call, no tool loop
    logger.debug("find_url: no deterministic match for %r at %s, trying LLM fallback",
                 title[:40], merchant)
    url_list = "\n".join(f"  {m_name}: {m_url}" for m_name, m_url in merchant_urls[:15])
    prompt = _PICK_URL_PROMPT.format(title=title, merchant=merchant, url_list=url_list)

    try:
        response = await llm.complete([{"role": "user", "content": prompt}])
        content = (response.content or "").strip()
        if content.startswith("http"):
            logger.debug("find_url: LLM fallback matched %r → %s", title[:40], content[:80])
            return content
    except Exception as exc:
        logger.debug("find_url: LLM fallback failed: %s", exc)

    _dump_snap(title, merchant, snap, reason="no_match", merchant_urls=merchant_urls)
    logger.debug("find_url: no URL found for %r at %s", title[:40], merchant)
    return ""


def _dump_snap(
    title: str,
    merchant: str,
    snap: str,
    reason: str,
    merchant_urls: list[tuple[str, str]] | None = None,
) -> None:
    """Write full aria snapshot + parsed URLs to /tmp for debugging label misses."""
    import re
    import tempfile

    safe = re.sub(r"[^\w]+", "_", title[:40]).strip("_")
    path = f"{tempfile.gettempdir()}/find_url_miss_{safe}.txt"
    try:
        with open(path, "w") as f:
            f.write(f"TITLE:    {title}\n")
            f.write(f"MERCHANT: {merchant}\n")
            f.write(f"REASON:   {reason}\n")
            if merchant_urls:
                f.write(f"\nPARSED MERCHANT URLs ({len(merchant_urls)}):\n")
                for m_name, m_url in merchant_urls:
                    f.write(f"  {m_name}: {m_url}\n")
            f.write(f"\n{'='*60}\nFULL SNAP ({len(snap)} chars):\n{'='*60}\n")
            f.write(snap)
        logger.debug("find_url: snap dumped → %s", path)
    except Exception as exc:
        logger.debug("find_url: snap dump failed: %s", exc)


def _extract_merchant_urls(snap: str) -> list[tuple[str, str]]:
    """
    Parse the detail panel aria snapshot for merchant → URL pairs.

    Extracts from two patterns in Google Shopping's detail panel:

    Pattern A — per-listing offer (specific to the clicked product):
        link "{Merchant} Current price: $XX.XX ..."
          /url: https://retailer.com/product/...

    Pattern B — "Compare prices" section (shared across product family):
        link "for from {Merchant}":
          /url: https://retailer.com/product/...

    Pattern A results come first (more specific). Returns deduplicated
    (merchant_name, url) tuples.
    """
    import re

    lines = snap.splitlines()
    offer_results: list[tuple[str, str]] = []  # Pattern A — per-listing
    compare_results: list[tuple[str, str]] = []  # Pattern B — compare prices
    seen_urls: set[str] = set()

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Pattern A: link "{Merchant} Current price: $..."
        if 'link "' in stripped and "Current price" in stripped:
            match = re.search(r'link "(.+?)\s+Current price', stripped)
            if not match:
                continue
            merchant_name = match.group(1).strip()

            for j in range(i + 1, min(i + 5, len(lines))):
                ns = lines[j].strip()
                if "/url:" not in ns:
                    continue
                url = ns.split("/url:", 1)[1].strip()
                if url.startswith("http") and "google.com" not in url and url not in seen_urls:
                    seen_urls.add(url)
                    offer_results.append((merchant_name, url))
                    break
            continue

        # Pattern B: link "for from {Merchant}" or "for was $X from {Merchant}"
        if 'link "for' in stripped and "from" in stripped:
            match = re.search(r'from\s+(.+?)(?:\s*")', stripped)
            if not match:
                continue
            merchant_name = match.group(1).strip()

            for j in range(i + 1, min(i + 5, len(lines))):
                ns = lines[j].strip()
                if "/url:" not in ns:
                    continue
                url = ns.split("/url:", 1)[1].strip()
                if url.startswith("http") and "google.com" not in url and url not in seen_urls:
                    seen_urls.add(url)
                    compare_results.append((merchant_name, url))
                    break

    # Per-listing offers first (more specific), then compare prices
    return offer_results + compare_results


# ---------------------------------------------------------------------------
# Aria snapshot parsing
# ---------------------------------------------------------------------------

def _extract_button_labels(snap: str) -> list[str]:
    """Extract raw button label strings from aria snapshot for organic listings."""
    labels: list[str] = []
    for line in snap.splitlines():
        stripped = line.strip()
        if "Current Price:" in stripped and "button" in stripped:
            label = stripped
            for prefix in ("- 'button \"", "- button \"", "'button \"", 'button "'):
                if label.startswith(prefix):
                    label = label[len(prefix):]
                    break
            for suffix in ("\"':", '":', '"\'', '"'):
                if label.endswith(suffix):
                    label = label[:-len(suffix)]
                    break
            labels.append(label)
    return labels



def _filter_aria_snapshot(snap: str) -> str:
    """
    Parse an aria snapshot into two clearly labelled sections:
    - FEATURED DEALS: direct retailer URLs + price, one per line
    - ORGANIC LISTINGS: button text with title/price/merchant/condition, no URL

    This structured format lets the LLM extract deal fields without ambiguity.
    """
    lines = snap.splitlines()
    featured: list[str] = []
    organics: list[str] = []
    seen_urls: set[str] = set()

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Featured listing: retailer URL line
        if "/url:" in stripped:
            url = stripped.split("/url:", 1)[1].strip()
            if not (url.startswith("http") and "google.com" not in url):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # Look ahead up to 8 lines for the adjacent price/merchant text line
            price_text = ""
            for j in range(i + 1, min(i + 9, len(lines))):
                ns = lines[j].strip()
                if "text:" in ns and "$" in ns:
                    price_text = ns.split("text:", 1)[1].strip()
                    break

            if price_text:
                featured.append(f"URL: {url} | {price_text}")

        # Organic listing: button element with product info
        elif "Current Price:" in stripped and "button" in stripped:
            btn = stripped[:300]
            for prefix in ("- 'button \"", "- button \"", "'button \"", 'button "'):
                if btn.startswith(prefix):
                    btn = btn[len(prefix):]
                    break
            for suffix in ("\"':", '":', '"\'', '"'):
                if btn.endswith(suffix):
                    btn = btn[: -len(suffix)]
                    break
            organics.append(btn)

    parts: list[str] = []
    if featured:
        parts.append("FEATURED DEALS (direct retailer URLs already resolved):")
        for idx, f in enumerate(featured, 1):
            parts.append(f"  {idx}. {f}")
    if organics:
        parts.append("\nORGANIC LISTINGS (no URL yet — call find_url to resolve):")
        for idx, o in enumerate(organics, 1):
            parts.append(f"  {idx}. {o}")

    return "\n".join(parts)



# ---------------------------------------------------------------------------
# Browserbase session management
# ---------------------------------------------------------------------------

_MAX_SESSION_RETRIES = 5


async def _create_session(api_key: str, project_id: str, proxies: bool = False) -> tuple[str, str]:
    """Returns (session_id, connect_url). Retries on 429 using retry-after header."""
    payload: dict = {"projectId": project_id, "keepAlive": True, "timeout": 3600}
    if proxies:
        payload["proxies"] = True
    for attempt in range(_MAX_SESSION_RETRIES):
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_BROWSERBASE_API}/sessions",
                headers={"X-BB-API-Key": api_key, "Content-Type": "application/json"},
                json=payload,
            )
        if resp.status_code == 429:
            if attempt == _MAX_SESSION_RETRIES - 1:
                resp.raise_for_status()
            retry_after = int(resp.headers.get("retry-after", 2))
            wait = retry_after * (2 ** attempt)
            logger.debug("browser_agent: 429, retrying in %ds (attempt %d)", wait, attempt + 1)
            await asyncio.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        return data["id"], data["connectUrl"]
    resp.raise_for_status()
    return "", ""  # unreachable


async def _terminate_session(api_key: str, session_id: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{_BROWSERBASE_API}/sessions/{session_id}",
                headers={"X-BB-API-Key": api_key, "Content-Type": "application/json"},
                json={"status": "REQUEST_RELEASE"},
            )
        logger.debug("browser_agent: terminated session %s", session_id)
    except Exception as exc:
        logger.debug("browser_agent: failed to terminate session %s: %s", session_id, exc)
