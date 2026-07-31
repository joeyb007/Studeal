"""Measure the intent↔listing cosine distribution that sets §8b's τ.

For each watchlist with an intent vector, prints similarity percentiles
against the fresh pool plus the count above candidate thresholds. τ should
land where on-topic listings sit above it and off-topic ones below — read the
per-watchlist top-5 titles to judge that by eye.

Read-only. No LLM calls.

PREREQUISITE: scripts/backfill_watchlist_intent.py must have run first, or the
intent vectors are still bare-query embeddings and this measures the wrong
distribution.

Usage (from the host shell — note the port-mapped DATABASE_URL):
    DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5433/dealbot" \\
        venv/bin/python scripts/measure_intent_similarity.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from sqlalchemy import text  # noqa: E402

from dealbot.db.database import get_async_session  # noqa: E402
from dealbot.lifecycle import stale_cutoff  # noqa: E402

CANDIDATE_TAUS = [0.30, 0.40, 0.50, 0.60, 0.70, 0.80]


async def run() -> None:
    async with get_async_session() as session:
        rows = (await session.execute(text("""
            SELECT w.id, w.name,
                   1 - (l.embedding <=> w.intent_embedding) AS sim,
                   l.title
            FROM watchlists w
            JOIN listings l ON l.embedding IS NOT NULL
            WHERE w.intent_embedding IS NOT NULL
              AND l.last_seen_at >= :cutoff
            ORDER BY w.id, sim DESC
        """), {"cutoff": stale_cutoff()})).all()

    by_watchlist: dict[int, list] = {}
    names: dict[int, str] = {}
    for wl_id, name, sim, title in rows:
        by_watchlist.setdefault(wl_id, []).append((float(sim), title))
        names[wl_id] = name

    if not by_watchlist:
        print("No (intent, listing) pairs — run the intent backfill and check "
              "that the pool has embedded, fresh listings.")
        return

    print(f"{len(by_watchlist)} watchlists × fresh embedded pool\n")
    for wl_id, sims in by_watchlist.items():
        values = [s for s, _ in sims]
        n = len(values)
        pct = lambda p: values[min(n - 1, int(n * p))]  # noqa: E731
        print(f"[{wl_id}] {names[wl_id]}  (n={n})")
        print(f"  p50={pct(0.50):.3f}  p90={pct(0.10):.3f}  p99={pct(0.01):.3f}  max={values[0]:.3f}")
        print("  count ≥ τ: " + "  ".join(
            f"{tau:.2f}→{sum(1 for v in values if v >= tau)}" for tau in CANDIDATE_TAUS
        ))
        for sim, title in sims[:5]:
            print(f"    {sim:.3f}  {title[:70]}")
        print()


if __name__ == "__main__":
    asyncio.run(run())
