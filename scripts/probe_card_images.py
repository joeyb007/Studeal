"""Diagnose thumbnail capture per marketplace: anchor patterns, per-card img
candidates (all of them, with attrs + layout size), so capture specs and the
pick heuristic can be tuned from evidence.

Run:  AWS_PROFILE=studeal-deploy python scripts/probe_card_images.py canada_computers newegg_ca ...
"""

from __future__ import annotations

import asyncio
import json
import sys

from dealbot.agents.marketplace_router import CONFIG_BY_KEY
from dealbot.scrapers.browser_session import build_browser_session, BrowserbaseSession

QUERY = "laptop"

_DISCOVER_JS = """
() => {
  const counts = {};
  for (const a of document.querySelectorAll('a[href]')) {
    const path = new URL(a.href, location.href).pathname;
    const seg = '/' + (path.split('/')[1] || '') + '/';
    if (!counts[seg]) counts[seg] = { n: 0, withImg: 0, sample: '' };
    counts[seg].n++;
    if (a.closest('div,li,article')?.querySelector('img')) counts[seg].withImg++;
    if (!counts[seg].sample) counts[seg].sample = path.slice(0, 70);
  }
  return Object.entries(counts).filter(([, v]) => v.n >= 5)
    .sort((a, b) => b[1].n - a[1].n).slice(0, 10);
}
"""

_CARD_IMGS_JS = """
(sel) => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll(sel)) {
    if (seen.has(a.href) || out.length >= 8) continue;
    seen.add(a.href);
    let c = a;
    for (let i = 0; i < 6; i++) {
      const p = c.parentElement;
      if (!p) break;
      const hs = new Set([...p.querySelectorAll(sel)].map(x => x.href));
      hs.delete('');
      if (hs.size > 1) break;
      c = p;
    }
    const imgs = [...c.querySelectorAll('img')].slice(0, 5).map(im => ({
      src: (im.getAttribute('src') || '').slice(0, 90),
      dataSrc: (im.getAttribute('data-src') || im.getAttribute('data-lazy-src') || '').slice(0, 90),
      srcset: (im.getAttribute('srcset') || im.getAttribute('data-srcset') || '').slice(0, 70),
      w: im.offsetWidth, h: im.offsetHeight,
    }));
    out.push({ href: a.href.slice(0, 80), imgs });
  }
  return out;
}
"""


async def probe(key: str) -> None:
    cfg = CONFIG_BY_KEY[key]
    entry = cfg.build_search_url(QUERY)
    backend = cfg.backend or "agentcore"
    session = (
        BrowserbaseSession(proxies=cfg.browserbase_proxies)
        if backend == "browserbase" else build_browser_session("agentcore")
    )
    async with session as s:
        page = s.page
        root = entry.split("/", 3)
        try:
            await page.goto(f"{root[0]}//{root[2]}", wait_until="domcontentloaded",
                            timeout=20_000, referer=cfg.entry_referer)
        except Exception:
            pass
        await page.goto(entry, wait_until="domcontentloaded", timeout=20_000,
                        referer=cfg.entry_referer)
        await asyncio.sleep(4)
        print(f"\n===== {key} · {page.url[:80]}")
        if cfg.listing_href_pattern:
            sel = f'a[href*="{cfg.listing_href_pattern}"]'
            cards = await page.evaluate(_CARD_IMGS_JS, sel)
            print(json.dumps(cards, indent=1)[:3500])
        else:
            segs = await page.evaluate(_DISCOVER_JS)
            print("path segments:", json.dumps(segs, indent=1)[:1500])
            if segs:
                sel = f'a[href*="{segs[0][0]}"]'
                cards = await page.evaluate(_CARD_IMGS_JS, sel)
                print("cards via", sel, json.dumps(cards, indent=1)[:2500])


async def main() -> None:
    for key in sys.argv[1:]:
        try:
            await probe(key)
        except Exception as exc:
            print(f"{key} FAILED: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
