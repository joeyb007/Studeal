"""Hunt event simulator — drive Mission Control without API spend.

Publishes a scripted, realistically-timed hunt event sequence through the
real RedisEventPublisher and the real event schema (it cannot drift from the
contract). Use for frontend development and demo-video rehearsal.

Usage:
    venv/bin/python scripts/simulate_hunt.py --watchlist-id 9
    venv/bin/python scripts/simulate_hunt.py --watchlist-id 9 --speed 4 --loop

Prereqs: docker-compose redis up (host port 6380 by default).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import itertools
import struct
import zlib

from dealbot.events.publisher import RedisEventPublisher
from dealbot.events.schema import (
    AlertCreated,
    ExplorerError,
    ExplorerScreenshot,
    ExplorerTurn,
    ExtractionSubmitted,
    HuntFinished,
    HuntPersisted,
    HuntStarted,
    QueriesPlanned,
)

QUERIES = [
    "herman miller aeron toronto used",
    "aeron chair GTA",
    "herman miller office chair",
]

MARKETPLACE_COLORS = {
    "kijiji": (91, 60, 196),
    "ebay": (30, 110, 60),
    "craigslist": (150, 70, 40),
}

TURNS = [
    ("kijiji", "https://www.kijiji.ca/b-toronto/aeron", "typed 'herman miller aeron' into search", "results page loaded, 24 cards visible"),
    ("kijiji", "https://www.kijiji.ca/b-toronto/aeron/page-1", "scrolled to load more listings", "8 new cards appeared"),
    ("kijiji", "https://www.kijiji.ca/b-toronto/aeron/page-2", "clicked 'Next' pagination control", "page 2 loaded, 22 cards"),
    ("ebay", "https://www.ebay.ca/sch/aeron", "submitted search from home page", "grid view, 48 results"),
    ("ebay", "https://www.ebay.ca/sch/aeron?_pgn=2", "clicked page 2", "37 results, prices in CAD"),
    ("craigslist", "https://toronto.craigslist.org/search/aeron", "opened furniture category search", "31 postings listed"),
]


def _tiny_png(rgb: tuple[int, int, int], width: int = 320, height: int = 200) -> str:
    """Minimal solid-color PNG as a data URL — stdlib only, no Pillow."""
    row = b"\x00" + bytes(rgb) * width
    raw = row * height

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _screenshot_for(marketplace: str, turn: int) -> str:
    r, g, b = MARKETPLACE_COLORS.get(marketplace, (80, 80, 80))
    lift = min(turn * 18, 90)  # visibly different frame per turn
    return _tiny_png((min(r + lift, 255), min(g + lift, 255), min(b + lift, 255)))


async def run_once(pub: RedisEventPublisher, watchlist_id: int, hunt_id: int, speed: float) -> None:
    async def pause(seconds: float) -> None:
        await asyncio.sleep(seconds / speed)

    env = {"hunt_id": hunt_id, "watchlist_id": watchlist_id}

    await pub.publish(HuntStarted(**env))
    await pause(1.5)
    await pub.publish(QueriesPlanned(**env, queries=QUERIES))
    await pause(2.0)

    for turn_number, (marketplace, url, action, result) in enumerate(TURNS, start=1):
        query = QUERIES[turn_number % len(QUERIES)]
        await pub.publish(ExplorerTurn(
            **env, query=query, marketplace=marketplace, turn=turn_number,
            url=url, action=action, result=result,
        ))
        await pause(1.0)
        await pub.publish(ExplorerScreenshot(
            **env, query=query, marketplace=marketplace, turn=turn_number,
            image_data_url=_screenshot_for(marketplace, turn_number),
        ))
        await pause(1.5)
        await pub.publish(ExtractionSubmitted(**env, query=query, marketplace=marketplace))
        await pause(2.5)

    await pub.publish(ExplorerError(
        **env, query=QUERIES[0], marketplace="craigslist",
        error="stale element on click — re-observing",
    ))
    await pause(2.0)
    await pub.publish(HuntPersisted(
        **env, offer_count=47, persisted_count=38, new_for_watchlist=3,
    ))
    await pause(1.0)
    for alert_id, (title, price, score) in enumerate([
        ("Herman Miller Aeron Size B — fully loaded", 420.0, 0.94),
        ("Aeron chair, posturefit, great condition", 495.0, 0.88),
        ("Herman Miller Aeron (needs new casters)", 340.0, 0.71),
    ], start=1):
        await pub.publish(AlertCreated(
            **env, alert_id=alert_id, listing_id=alert_id * 11,
            title=title, price=price, currency="CAD", score=score,
            url=f"https://www.kijiji.ca/v-chairs/aeron/{alert_id}",
        ))
        await pause(1.2)
    await pub.publish(HuntFinished(**env, status="succeeded", duration_s=84.3, error=None))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watchlist-id", type=int, required=True)
    parser.add_argument("--speed", type=float, default=1.0, help="time multiplier; 4 = 4x faster")
    parser.add_argument("--loop", action="store_true", help="repeat forever (new hunt_id each pass)")
    parser.add_argument("--redis-url", default="redis://localhost:6380/0",
                        help="host-side compose redis (default localhost:6380)")
    args = parser.parse_args()

    pub = RedisEventPublisher(redis_url=args.redis_url)
    try:
        for pass_number in itertools.count():
            hunt_id = 90_000 + pass_number  # synthetic range, unmistakably fake
            print(f"simulate_hunt: publishing hunt {hunt_id} → watchlist {args.watchlist_id}")
            await run_once(pub, args.watchlist_id, hunt_id, args.speed)
            if not args.loop:
                break
            await asyncio.sleep(3.0 / args.speed)
    finally:
        await pub.aclose()


if __name__ == "__main__":
    asyncio.run(main())
