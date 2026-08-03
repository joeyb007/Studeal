"""Probe: can we deterministically capture listing thumbnails per marketplace?

Verifies the assumptions behind the image-capture feature BEFORE building it:
  1. Do listing-card anchors contain an <img> descendant at all, or is the
     thumbnail a CSS background-image (invisible to attribute capture)?
  2. Is the raw `src` ATTRIBUTE a real CDN URL, or a lazy-load placeholder
     with the real URL only in data-src/srcset/currentSrc?
  3. URL fidelity: do the anchor hrefs the extractor copies out of
     `snapshot_page` match what the DOM reports, so offer→img association
     can be a pure lookup (no LLM in the loop)?

Run:  python scripts/probe_listing_images.py
Uses the same LocalPlaywrightSession + FB_STATE_PATH auth as real hunts.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from dealbot.agents.perception import snapshot_page  # noqa: E402
from dealbot.scrapers.browser_session import LocalPlaywrightSession  # noqa: E402

QUERY = "office chair"
FB_CITY = os.environ.get("FB_MARKETPLACE_CITY", "toronto")

SITES = [
    {
        "key": "kijiji",
        "url": f"https://www.kijiji.ca/b-buy-sell/{QUERY.replace(' ', '-')}/k0c10",
        "anchor_sel": 'a[href*="/v-"]',
        "referer": None,
    },
    {
        "key": "ebay",
        "url": f"https://www.ebay.ca/sch/i.html?_nkw={QUERY.replace(' ', '+')}",
        "anchor_sel": 'a[href*="/itm/"]',
        "referer": None,
        # Direct headless entry trips eBay's bot wall; landing on the homepage
        # first (as a navigating hunt does) usually doesn't.
        "warmup": "https://www.ebay.ca/",
    },
    {
        "key": "fb_marketplace",
        "url": f"https://www.facebook.com/marketplace/{FB_CITY}/search/?query={QUERY.replace(' ', '%20')}",
        "anchor_sel": 'a[href*="/marketplace/item/"]',
        "referer": "https://www.google.com/",
    },
]

# Per-card ground truth, straight from the live DOM. The image rarely sits
# INSIDE the listing anchor (Kijiji title links are text-only) — so climb to
# the card container: the smallest ancestor still holding exactly ONE card
# anchor. That uniqueness rule is also the deterministic association the real
# capture would use, so its hit-rate here IS the feature's hit-rate.
CARD_JS = """
(sel) => {
  const seen = new Set();
  const out = [];
  for (const a of document.querySelectorAll(sel)) {
    const href = (a.getAttribute('href') || '').split('?')[0];
    if (!href || seen.has(href)) continue;
    seen.add(href);

    let container = a;
    for (let i = 0; i < 6; i++) {
      const p = container.parentElement;
      if (!p) break;
      const anchorsInParent = new Set(
        [...p.querySelectorAll(sel)].map(x => (x.getAttribute('href') || '').split('?')[0]),
      );
      if (anchorsInParent.size > 1) break;   // climbed into a multi-card region
      container = p;
    }

    const img = container.querySelector('img');
    let bg = null;
    if (!img) {
      for (const el of [container, ...container.querySelectorAll('*')].slice(0, 30)) {
        const b = getComputedStyle(el).backgroundImage;
        if (b && b !== 'none' && b.includes('url(')) { bg = b.slice(0, 120); break; }
      }
    }
    out.push({
      href: href.slice(0, 160),
      img: img ? {
        srcAttr: (img.getAttribute('src') || '').slice(0, 160),
        dataSrc: (img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || '').slice(0, 120),
        hasSrcset: !!img.getAttribute('srcset'),
        currentSrc: (img.currentSrc || '').slice(0, 160),
        naturalWidth: img.naturalWidth,
      } : null,
      bg,
    });
    if (out.length >= 24) break;
  }
  return out;
}
"""

# Debug: how many anchors match each candidate href pattern on this page.
PATTERN_JS = """
() => {
  const counts = {};
  for (const a of document.querySelectorAll('a[href]')) {
    const h = a.getAttribute('href') || '';
    for (const p of ['/marketplace/item/', '/itm/', '/v-', '/marketplace/']) {
      if (h.includes(p)) counts[p] = (counts[p] || 0) + 1;
    }
  }
  return counts;
}
"""


def is_real(url: str) -> bool:
    """A usable CDN URL, not a placeholder pixel or inline data blob."""
    return url.startswith("http") and not url.startswith("data:")


async def probe(site: dict) -> None:
    print(f"\n{'=' * 70}\n{site['key']}  →  {site['url']}\n{'=' * 70}")
    storage = os.environ.get("FB_STATE_PATH") if site["key"] == "fb_marketplace" else None
    async with LocalPlaywrightSession(storage_state=storage) as session:
        page = session.page
        try:
            if site.get("warmup"):
                await page.goto(site["warmup"], timeout=45_000, wait_until="domcontentloaded")
                await page.wait_for_timeout(2_500)
            await page.goto(site["url"], referer=site["referer"], timeout=45_000, wait_until="domcontentloaded")
        except Exception as exc:
            print(f"  NAV FAILED: {exc}")
            return
        await page.wait_for_timeout(6_000)
        # Nudge lazy-loaders the way a scrolling hunt does.
        for _ in range(2):
            await page.mouse.wheel(0, 900)
            await page.wait_for_timeout(2_000)
        await page.mouse.wheel(0, -1800)
        await page.wait_for_timeout(2_000)

        title = await page.title()
        cards = await page.evaluate(CARD_JS, site["anchor_sel"])
        print(f"  page title: {title[:80]!r}")
        print(f"  cards found: {len(cards)}")
        if not cards:
            counts = await page.evaluate(PATTERN_JS)
            print(f"  NO CARDS — href pattern counts on page: {counts}")
            body = await page.evaluate("() => document.body.innerText.slice(0, 300)")
            print(f"  body head: {body!r}")
            return

        with_img = [c for c in cards if c["img"]]
        bg_only = [c for c in cards if not c["img"] and c["bg"]]
        no_visual = [c for c in cards if not c["img"] and not c["bg"]]
        src_real = [c for c in with_img if is_real(c["img"]["srcAttr"])]
        cur_real = [c for c in with_img if is_real(c["img"]["currentSrc"])]
        lazy_only = [c for c in with_img if not is_real(c["img"]["srcAttr"]) and is_real(c["img"]["currentSrc"])]
        loaded = [c for c in with_img if c["img"]["naturalWidth"] >= 64]

        n = len(cards)
        print(f"  <img> descendant:        {len(with_img)}/{n}")
        print(f"  background-image only:   {len(bg_only)}/{n}")
        print(f"  no visual found:         {len(no_visual)}/{n}")
        print(f"  src ATTR is real URL:    {len(src_real)}/{len(with_img) or 1}")
        print(f"  currentSrc is real URL:  {len(cur_real)}/{len(with_img) or 1}")
        print(f"  lazy-only (attr fake, currentSrc real): {len(lazy_only)}")
        print(f"  actually loaded (naturalWidth>=64):     {len(loaded)}/{len(with_img) or 1}")
        for c in (src_real or cur_real)[:2]:
            print(f"    sample: {c['img']['srcAttr'] or c['img']['currentSrc']}")

        # URL fidelity: does snapshot_page carry the same hrefs the DOM has?
        try:
            snap = await snapshot_page(page)
            snap_hrefs = {
                (ref.attributes.get("href") or "").split("?")[0]
                for ref in snap.element_map.values()
                if ref.attributes.get("href")
            }
            matched = sum(
                1 for c in cards
                if any(h and (h == c["href"] or h.endswith(c["href"]) or c["href"].endswith(h)) for h in snap_hrefs)
            )
            print(f"  snapshot href fidelity:  {matched}/{n} card hrefs present in element_map")
        except Exception as exc:
            print(f"  snapshot_page failed: {exc}")


async def main() -> None:
    for site in SITES:
        await probe(site)


if __name__ == "__main__":
    asyncio.run(main())
