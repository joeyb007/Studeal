"""Verify the multi-img capture against live SERPs: raw candidates (before
whitelist) + the picker's chosen URL per card, so cdn_hosts can be set from
evidence and the badge/srcset fixes confirmed.

Run:  AWS_PROFILE=studeal-deploy python scripts/verify_capture.py newegg_ca openbox_ca bestbuy_outlet
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from dealbot.agents.image_capture import _CAPTURE_JS, _MAX_CARDS, capture_card_images, spec_for
from dealbot.agents.marketplace_router import CONFIG_BY_KEY
from dealbot.scrapers.browser_session import BrowserbaseSession, build_browser_session

QUERY = "laptop"


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
        await asyncio.sleep(5)
        print(f"\n===== {key} · {page.url[:80]}")
        pattern = cfg.listing_href_pattern or "/p/"
        sel = f'a[href*="{pattern}"]'
        raw = await page.evaluate(_CAPTURE_JS, [sel, 6])
        for card in raw[:5]:
            print(" card:", card["href"][:70])
            for im in card["imgs"][:3]:
                print(f"   img w={im['w']}x{im['h']} src={im['srcAttr'][:60]!r} srcset={im['srcset'][:60]!r}")
        spec = spec_for(key)
        if spec:
            result = await capture_card_images(page, spec, key)
            print(f" capture_card_images → {len(result)} urls")
            for k, v in list(result.items())[:3]:
                print("   ", v[:80])
        else:
            print(" (no spec yet)")


async def main() -> None:
    for key in sys.argv[1:]:
        try:
            await probe(key)
        except Exception as exc:
            print(f"{key} FAILED: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
