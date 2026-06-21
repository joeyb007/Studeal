"""One-time Facebook auth helper for the capability spike.

Usage:
  ./venv/bin/python -m tests.evals.fb_auth_helper

Pops a visible Chromium window at facebook.com/login.
You log in MANUALLY (username + password + any 2FA challenge).
Once you're on the FB home feed, press Enter in this terminal.
The cookies + localStorage snapshot saves to tests/evals/_fb_state.json.

The spike's FB Marketplace case then loads that file to start sessions with
your logged-in identity. State typically survives 1-4 weeks until FB
invalidates the session.

ToS note: scraping FB Marketplace via your personal account violates FB's
Terms of Service. For spike-scale activity (1 hunt every few minutes) the
practical detection risk is low, but a dedicated burner account is the
right answer if this grows into production scale.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

STATE_PATH = Path(__file__).resolve().parent / "_fb_state.json"
LOGIN_URL = "https://www.facebook.com/login"


async def main() -> None:
    print("Opening Chromium at facebook.com/login...")
    print("Log in manually. When you see the FB home feed (or marketplace), "
          "switch back here and press Enter.")
    print()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(LOGIN_URL)

        # Block on user input — wait for the human to finish logging in.
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, sys.stdin.readline)

        await ctx.storage_state(path=str(STATE_PATH))
        print(f"\n✓ Storage state saved to {STATE_PATH}")
        print("  (cookies + localStorage, all sites — not just facebook.com)")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
