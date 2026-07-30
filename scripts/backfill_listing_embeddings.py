"""Backfill embeddings for listings persisted before the embedding column.

Idempotent and resumable: only rows with embedding IS NULL are touched, so
interrupting and re-running is safe. Creates the ivfflat index afterward —
index creation needs rows to train on, which is why it isn't in migration 0023.

Usage:
    venv/bin/python scripts/backfill_listing_embeddings.py --dry-run
    venv/bin/python scripts/backfill_listing_embeddings.py --batch 100
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

from sqlalchemy import func, select, text  # noqa: E402

from dealbot.db.database import get_async_session  # noqa: E402
from dealbot.db.models import Listing  # noqa: E402
from dealbot.llm.embeddings import embed_texts  # noqa: E402

_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_listings_embedding "
    "ON listings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
)


def _embed_text_for(row: Listing) -> str:
    """Must match persistence.listings.listing_embed_text — same composition,
    but sourced from a DB row rather than an Offer."""
    return f"{row.title} | {row.condition} | {row.marketplace} | {row.location or ''}"


async def run(batch_size: int, dry_run: bool) -> None:
    async with get_async_session() as session:
        pending = (await session.execute(
            select(func.count()).select_from(Listing).where(Listing.embedding.is_(None))
        )).scalar_one()
    print(f"listings needing embeddings: {pending}")
    if dry_run:
        print("dry run — no writes, no index")
        return

    done = 0
    while pending:
        async with get_async_session() as session:
            rows = (await session.execute(
                select(Listing).where(Listing.embedding.is_(None)).limit(batch_size)
            )).scalars().all()
            if not rows:
                break
            vectors = await embed_texts([_embed_text_for(r) for r in rows])
            embedded = 0
            for row, vector in zip(rows, vectors):
                if vector:
                    row.embedding = vector
                    embedded += 1
            await session.commit()
        done += embedded
        print(f"  embedded {done}/{pending}")
        if embedded == 0:
            print("  no vectors returned for this batch — stopping "
                  "(embedding backend down?) to avoid a spin loop")
            break

    async with get_async_session() as session:
        await session.execute(text(_INDEX_SQL))
        await session.commit()
    print("ivfflat index ensured")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.batch, args.dry_run))


if __name__ == "__main__":
    main()
