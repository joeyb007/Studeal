"""Seed house watchlists that keep the shared listings pool warm.

Daily Drops is only as good as the pool behind it, and a pool fed solely by
signups is empty on launch day. These are ordinary watchlists owned by a system
user — the existing scheduler hunts them on cadence with no special-casing.

Idempotent: safe to run on every deploy.

Usage:
    venv/bin/python scripts/seed_house_watchlists.py
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from sqlalchemy import select  # noqa: E402

from dealbot.api.auth import hash_password  # noqa: E402
from dealbot.db.database import get_async_session  # noqa: E402
from dealbot.db.models import User, Watchlist  # noqa: E402
from dealbot.schemas import WatchlistContext  # noqa: E402

HOUSE_EMAIL = "house@studeal.internal"

# (display name, product query, max budget CAD) — the categories students hunt.
HOUSE_CATEGORIES: list[tuple[str, str, float]] = [
    ("Laptops", "used laptop", 1200.0),
    ("Headphones", "noise cancelling headphones", 400.0),
    ("Monitors", "computer monitor", 400.0),
    ("Desk chairs", "ergonomic office chair", 700.0),
    ("Phones", "unlocked smartphone", 900.0),
    ("Tablets", "tablet ipad", 700.0),
    ("Mechanical keyboards", "mechanical keyboard", 200.0),
    ("Bikes", "commuter bicycle", 600.0),
    ("Textbooks", "university textbook", 150.0),
    ("Printers", "home printer", 250.0),
    ("Mice", "wireless mouse", 120.0),
    ("Webcams", "webcam 1080p", 150.0),
    ("SSDs", "internal ssd 1tb", 200.0),
    ("Standing desks", "standing desk", 600.0),
    ("Backpacks", "laptop backpack", 150.0),
]


async def _get_or_create_house_user(session) -> User:
    user = (await session.execute(
        select(User).where(User.email == HOUSE_EMAIL)
    )).scalar_one_or_none()
    if user is not None:
        return user
    user = User(
        email=HOUSE_EMAIL,
        # No one logs in as this account; a random secret keeps the hash valid
        # without creating a usable credential.
        hashed_password=hash_password(secrets.token_urlsafe(32)),
        is_pro=True,          # bypass free-tier caps and daily cadence limits
    )
    session.add(user)
    await session.flush()
    return user


async def seed_house_watchlists(session) -> dict:
    """Create any missing house watchlists. Returns counts; never duplicates."""
    user = await _get_or_create_house_user(session)
    existing_names = {
        name for (name,) in (await session.execute(
            select(Watchlist.name).where(Watchlist.user_id == user.id)
        )).all()
    }

    created = 0
    for name, query, budget in HOUSE_CATEGORIES:
        if name in existing_names:
            continue
        context = WatchlistContext(
            product_query=query,
            max_budget=budget,
            condition=["used", "refurb"],
            keywords=[query],
        )
        session.add(Watchlist(
            user_id=user.id,
            name=name,
            context=context.model_dump_json(),
            hunting_enabled=True,
            hunt_frequency_minutes=1440,   # daily, explicit rather than tier-derived
        ))
        created += 1
    await session.commit()
    return {
        "user_id": user.id,
        "created": created,
        "existing": len(HOUSE_CATEGORIES) - created,
    }


async def main() -> None:
    async with get_async_session() as session:
        result = await seed_house_watchlists(session)
    print(f"house user id={result['user_id']} "
          f"created={result['created']} existing={result['existing']}")


if __name__ == "__main__":
    asyncio.run(main())
