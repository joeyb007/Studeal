from __future__ import annotations

import asyncio
import codecs
import logging
import os
import re
from dataclasses import dataclass
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
_PAGE_RENDER_WAIT_MS = 2_500
# Configurable via env — bump when upgrading Browserbase plan
BROWSERBASE_MAX_SESSIONS = int(os.environ.get("BROWSERBASE_MAX_SESSIONS", "3"))

_RE_MONEY = re.compile(r"\$([0-9]+(?:[,.][0-9]{1,2})?)")
_RE_PCT_OFF = re.compile(r"([0-9]+)%\s*off", re.IGNORECASE)


@dataclass
class OfferData:
    """A single retailer offer extracted from a Google Shopping aria snapshot."""

    merchant: str
    url: str
    sale_price: float | None = None
    listed_price: float | None = None
    discount_pct: float | None = None
    condition: str | None = None  # "refurbished" | "pre-owned" | "used" | None


def _parse_money(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None
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


def _extract_merchant_urls(snap: str) -> list[OfferData]:
    """Parse a Google Shopping aria snapshot for all retailer offer data.

    Three patterns (in priority order):

    A — "Best price" headline offer (has full price breakdown):
        link "Best price {Merchant} Current price is $X. N% off Old price was $Y ..."
          /url: https://retailer.com/...

    B — Per-listing offer (current price only):
        link "{Merchant} Current price: $X ..."
          /url: https://retailer.com/...

    C — "Compare prices" section:
        link "for from {Merchant}"
          /url: https://retailer.com/...

    Returns deduplicated OfferData list. Pattern A results first (richer data).
    """
    lines = snap.splitlines()
    best_results: list[OfferData] = []   # Pattern A — full price breakdown
    offer_results: list[OfferData] = []  # Pattern B — per-listing
    compare_results: list[OfferData] = [] # Pattern C — compare prices
    seen_urls: set[str] = set()

    def _find_url(start: int, window: int = 5) -> str | None:
        for j in range(start + 1, min(start + window, len(lines))):
            ns = lines[j].strip()
            if "/url:" not in ns:
                continue
            url = ns.split("/url:", 1)[1].strip()
            if url.startswith("http") and "google.com" not in url and url not in seen_urls:
                seen_urls.add(url)
                return url
        return None

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Pattern A: "Best price {Merchant} Current price is $X. N% off Old price was $Y"
        if 'link "Best price' in stripped and "Current price is" in stripped:
            m_name = re.search(r'Best price\s+(.+?)\s+Current price is', stripped)
            sale_m = re.search(r'Current price is \$([0-9]+(?:[,.][0-9]{1,2})?)', stripped)
            pct_m = _RE_PCT_OFF.search(stripped)
            was_m = re.search(r'Old price was \$([0-9]+(?:[,.][0-9]{1,2})?)', stripped)
            if m_name and sale_m:
                url = _find_url(i)
                if url:
                    sale = _parse_money(sale_m.group(1))
                    listed = _parse_money(was_m.group(1)) if was_m else None
                    pct = float(pct_m.group(1)) if pct_m else None
                    if not pct and sale and listed and listed > sale:
                        pct = round((listed - sale) / listed * 100, 1)
                    best_results.append(OfferData(
                        merchant=m_name.group(1).strip(),
                        url=url,
                        sale_price=sale,
                        listed_price=listed,
                        discount_pct=pct,
                    ))
            continue

        # Pattern B: "{Merchant} Current price: $X"
        if 'link "' in stripped and "Current price" in stripped and "Best price" not in stripped:
            m_name = re.search(r'link "(.+?)\s+Current price', stripped)
            sale_m = _RE_MONEY.search(stripped)
            was_m = re.search(r'[Ww]as\s*\$([0-9]+(?:[,.][0-9]{1,2})?)', stripped)
            if m_name:
                url = _find_url(i)
                if url:
                    sale = _parse_money(sale_m.group(1)) if sale_m else None
                    listed = _parse_money(was_m.group(1)) if was_m else None
                    pct = None
                    if sale and listed and listed > sale:
                        pct = round((listed - sale) / listed * 100, 1)
                    offer_results.append(OfferData(
                        merchant=m_name.group(1).strip(),
                        url=url,
                        sale_price=sale,
                        listed_price=listed,
                        discount_pct=pct,
                    ))
            continue

        # Pattern C: "for from {Merchant}"
        if 'link "for' in stripped and "from" in stripped:
            m_name = re.search(r'from\s+(.+?)(?:\s*")', stripped)
            if m_name:
                url = _find_url(i)
                if url:
                    compare_results.append(OfferData(
                        merchant=m_name.group(1).strip(),
                        url=url,
                    ))

    return best_results + offer_results + compare_results


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
# Tier 2 link resolution — Browserbase + aria snapshot
# ---------------------------------------------------------------------------

_tier2_sem: asyncio.Semaphore | None = None


def _get_tier2_sem() -> asyncio.Semaphore:
    """Lazily create semaphore respecting BROWSERBASE_MAX_SESSIONS env var."""
    global _tier2_sem
    if _tier2_sem is None:
        _tier2_sem = asyncio.Semaphore(BROWSERBASE_MAX_SESSIONS)
    return _tier2_sem


async def resolve_serper_url(google_url: str) -> OfferData | None:
    """Render a Google Shopping product URL via Browserbase and extract the
    best retailer offer from the fully-rendered aria tree.

    Called as Tier 2 fallback when httpx static-HTML parsing fails.
    Uses residential proxies (proxies=True) to bypass Google bot detection.

    Returns the cheapest OfferData with a direct URL, or None on failure.
    """
    api_key = os.environ.get("BROWSERBASE_API_KEY", "")
    project_id = os.environ.get("BROWSERBASE_PROJECT_ID", "")
    if not api_key or not project_id:
        logger.debug("resolve_serper_url: BROWSERBASE credentials not set")
        return None

    async with _get_tier2_sem():
        session_id = None
        try:
            session_id, connect_url = await _create_session(
                api_key, project_id, proxies=True,
            )
            async with async_playwright() as pw:
                browser = await pw.chromium.connect_over_cdp(connect_url)
                ctx = browser.contexts[0]
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()

                await page.goto(
                    google_url,
                    wait_until="domcontentloaded",
                    timeout=_PAGE_TIMEOUT,
                )
                await page.wait_for_timeout(_PAGE_RENDER_WAIT_MS)

                snap = await page.locator("body").aria_snapshot()
                await browser.close()

            if len(snap) < 300:
                logger.warning(
                    "resolve_serper_url: short snapshot (%d chars) — possible CAPTCHA",
                    len(snap),
                )
                return None

            offers = _extract_merchant_urls(snap)
            if not offers:
                logger.debug("resolve_serper_url: no offers extracted from snapshot")
                return None

            # Prefer the cheapest offer; fall back to first with a URL
            with_price = [o for o in offers if o.sale_price is not None]
            best = min(with_price, key=lambda o: o.sale_price) if with_price else offers[0]

            logger.info(
                "resolve_serper_url: %d offers found, best=%r url=%s price=$%s",
                len(offers), best.merchant, best.url[:70], best.sale_price,
            )
            return best

        except Exception as exc:
            logger.warning("resolve_serper_url: failed: %s", exc)
            return None
        finally:
            if session_id:
                await _terminate_session(api_key, session_id)


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
