"""Probe the curated marketplace lineup on the AgentCore browser backend.

For each marketplace: warm-up goto to the site root, then the SERP for a
fixed query, settled snapshot, and report the degenerate-page metrics we
calibrated on Browserbase (eBay soft-block shell = 18 elems / 399 chars;
healthy SERPs = 500+ elems). Also runs thumbnail capture to verify the
src-attribute fallback under media blocking.

Run:  AGENT_BROWSER_BACKEND=agentcore AWS_PROFILE=studeal-deploy \
      python scripts/probe_agentcore.py [query]
"""

from __future__ import annotations

import asyncio
import sys

from dealbot.agents.image_capture import capture_card_images, spec_for
from dealbot.agents.marketplace_router import CURATED_MARKETPLACES
from dealbot.agents.perception import snapshot_page
from dealbot.scrapers.browser_session import build_browser_session

QUERY = sys.argv[1] if len(sys.argv) > 1 else "macbook air"


async def settled_snapshot(page):
    snap = await snapshot_page(page)
    for _ in range(2):
        if len(snap.text) >= 800:
            break
        await asyncio.sleep(1.5)
        snap = await snapshot_page(page)
    return snap


async def probe_one(session, cfg) -> dict:
    page = session.page
    entry = cfg.build_search_url(QUERY)
    root = entry.split("/", 3)
    root_url = f"{root[0]}//{root[2]}"
    referer = cfg.entry_referer
    try:
        if root_url.rstrip("/") != entry.rstrip("/"):
            try:
                await page.goto(root_url, wait_until="domcontentloaded", timeout=20_000, referer=referer)
            except Exception:
                pass
        await page.goto(entry, wait_until="domcontentloaded", timeout=20_000, referer=referer)
    except Exception as exc:
        return {"marketplace": cfg.key, "error": f"goto: {type(exc).__name__}: {exc}"}

    snap = await settled_snapshot(page)
    images = {}
    spec = spec_for(cfg.key)
    if spec is not None:
        images = await capture_card_images(page, spec, cfg.key)
    return {
        "marketplace": cfg.key,
        "elems": len(snap.element_map),
        "chars": len(snap.text),
        "captcha": snap.captcha_detected,
        "images": len(images),
        "url": page.url[:90],
    }


async def main() -> None:
    async with build_browser_session("agentcore") as session:
        print(f"query: {QUERY!r}\n")
        for cfg in CURATED_MARKETPLACES:
            result = await probe_one(session, cfg)
            print(result)


if __name__ == "__main__":
    asyncio.run(main())
