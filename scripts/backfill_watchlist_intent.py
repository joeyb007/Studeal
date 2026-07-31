"""Re-embed watchlist intent vectors from the full intent document.

Rows created before Workstream C hold an embedding of the bare product_query.
This re-embeds them from compose_intent_document() so every watchlist's vector
is drawn from the same text distribution.

Unlike the listings backfill this cannot filter on IS NULL — the vectors exist,
they are just built from the wrong text. Re-running is safe; it re-does work.

Usage:
    venv/bin/python scripts/backfill_watchlist_intent.py --dry-run
    venv/bin/python scripts/backfill_watchlist_intent.py --batch 50
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

from dealbot.db.database import get_async_session  # noqa: E402
from dealbot.db.models import Watchlist  # noqa: E402
from dealbot.llm.embeddings import embed_texts  # noqa: E402
from dealbot.recsys.intent import compose_intent_document  # noqa: E402
from dealbot.schemas import WatchlistContext  # noqa: E402


def _document_for(watchlist: Watchlist) -> str:
    """Composed intent document, or "" if the row has no usable context."""
    if not watchlist.context:
        return ""
    try:
        context = WatchlistContext.model_validate_json(watchlist.context)
    except Exception:
        # One malformed row must never abort a backfill over the whole table.
        return ""
    return compose_intent_document(context)


def _needs_backfill(watchlist: Watchlist) -> bool:
    return bool(_document_for(watchlist).strip())


async def run(batch_size: int, dry_run: bool) -> None:
    async with get_async_session() as session:
        rows = list((await session.execute(select(Watchlist))).scalars().all())

    targets = [w for w in rows if _needs_backfill(w)]
    print(f"watchlists: {len(rows)} total, {len(targets)} with composable context")
    if dry_run:
        for watchlist in targets[:5]:
            print(f"  [{watchlist.id}] {watchlist.name}: {_document_for(watchlist)[:100]}")
        print("dry run — no writes")
        return

    done = 0
    for start in range(0, len(targets), batch_size):
        chunk = targets[start:start + batch_size]
        vectors = await embed_texts([_document_for(w) for w in chunk])
        async with get_async_session() as session:
            embedded = 0
            for watchlist, vector in zip(chunk, vectors):
                if not vector:
                    continue
                fresh = await session.get(Watchlist, watchlist.id)
                if fresh is not None:
                    fresh.intent_embedding = vector
                    embedded += 1
            await session.commit()
        done += embedded
        print(f"  re-embedded {done}/{len(targets)}")
        if embedded == 0:
            print("  no vectors returned — stopping (embedding backend down?)")
            break


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.batch, args.dry_run))


if __name__ == "__main__":
    main()
