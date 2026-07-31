"""Profile on/off ablation — does buyer_profile sharpen discovery or dilute it?

The profile is meant to make queries and rankings more *personal*. The failure
mode is that it makes them vaguer: chatty prose bleeding into search strings, or
the generator drifting off the product entirely. This prints both sides so the
difference is visible before it reaches users.

Read the output; there are no assertions. This is a smoke test, not the eval
campaign.

Fixtures are the default because watchlists created before Workstream C have no
buyer_profile. Once Scout has produced real ones, --from-db ablates those.

Usage:
    venv/bin/python scripts/sanity_profile_ablation.py
    venv/bin/python scripts/sanity_profile_ablation.py --from-db --limit 5
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from sqlalchemy import select  # noqa: E402

from dealbot.agents.query_generator import QueryGenerator  # noqa: E402
from dealbot.db.database import get_async_session  # noqa: E402
from dealbot.db.models import Watchlist  # noqa: E402
from dealbot.llm.openai_client import OpenAIClient  # noqa: E402
from dealbot.recsys.intent import compose_intent_document  # noqa: E402
from dealbot.schemas import WatchlistContext  # noqa: E402

# Profiles written to look like Scout output: 1-2 sentences, inferred from what
# a buyer would volunteer, never a restatement of the product query.
FIXTURES: list[tuple[str, WatchlistContext]] = [
    ("Laptop / commuting student", WatchlistContext(
        product_query="used laptop",
        max_budget=1200.0,
        condition=["used", "refurb"],
        buyer_profile=(
            "CS student who works on the train most days; values battery life "
            "and a comfortable keyboard over raw specs."
        ),
    )),
    ("Chair / back problems", WatchlistContext(
        product_query="ergonomic office chair",
        max_budget=700.0,
        condition=["used", "refurb"],
        buyer_profile=(
            "Remote worker with chronic lower-back pain; needs genuine lumbar "
            "adjustment and will pay more for it."
        ),
    )),
    ("Headphones / noisy library", WatchlistContext(
        product_query="noise cancelling headphones",
        max_budget=400.0,
        condition=["used", "refurb"],
        buyer_profile=(
            "Studies in a loud shared library; wants over-ear isolation for "
            "long sessions and dislikes earbuds."
        ),
    )),
    ("Monitor / colour work", WatchlistContext(
        product_query="computer monitor",
        max_budget=400.0,
        condition=["used", "refurb"],
        buyer_profile=(
            "Design student who edits photos; colour accuracy matters far more "
            "than refresh rate."
        ),
    )),
    ("Bike / winter commuter", WatchlistContext(
        product_query="commuter bicycle",
        max_budget=600.0,
        condition=["used"],
        buyer_profile=(
            "Rides to campus year-round including winter; wants fenders, "
            "durability, and low maintenance over light weight."
        ),
    )),
]


async def _ablate(label: str, with_profile: WatchlistContext, generator: QueryGenerator) -> None:
    without = with_profile.model_copy(update={"buyer_profile": None})

    print(f"\n{'=' * 74}\n{label}")
    print(f"profile: {with_profile.buyer_profile or '(none)'}")
    print(f"\n  intent doc WITHOUT: {compose_intent_document(without)}")
    print(f"  intent doc WITH:    {compose_intent_document(with_profile)}")

    queries_without = await generator.generate(without)
    queries_with = await generator.generate(with_profile)
    print(f"\n  queries WITHOUT: {queries_without}")
    print(f"  queries WITH:    {queries_with}")

    longest = max((len(q) for q in queries_with), default=0)
    baseline = max((len(q) for q in queries_without), default=0)
    if longest > baseline * 1.8 and longest > 40:
        print(f"  ⚠ longest query grew {baseline} → {longest} chars — possible prose leak")


async def _fixtures(generator: QueryGenerator) -> None:
    for label, context in FIXTURES:
        await _ablate(label, context, generator)


async def _from_db(generator: QueryGenerator, limit: int) -> None:
    async with get_async_session() as session:
        rows = list((await session.execute(
            select(Watchlist).where(Watchlist.context.isnot(None)).limit(limit * 4)
        )).scalars().all())

    ablated = 0
    for watchlist in rows:
        try:
            context = WatchlistContext.model_validate_json(watchlist.context)
        except Exception:
            continue
        if not (context.buyer_profile and context.buyer_profile.strip()):
            continue
        await _ablate(f"[{watchlist.id}] {watchlist.name}", context, generator)
        ablated += 1
        if ablated >= limit:
            break

    if ablated == 0:
        print("No watchlist has a buyer_profile yet — create one through Scout, "
              "or drop --from-db to ablate the fixtures.")


async def run(from_db: bool, limit: int) -> None:
    generator = QueryGenerator(llm=OpenAIClient())
    if from_db:
        await _from_db(generator, limit)
    else:
        await _fixtures(generator)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-db", action="store_true",
                        help="ablate real watchlists that have a buyer_profile")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(run(args.from_db, args.limit))


if __name__ == "__main__":
    main()
