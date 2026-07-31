"""Pool-metabolism policy — what "stale" and "purgeable" mean, in one place.

The listings pool is the system's belief about what is currently for sale,
with confidence decaying as sightings age. Staleness is measured on
`last_seen_at`: the fleet re-hunts continuously and `persist_offers` bumps it
on re-encounter, so still-appearing ⇒ live, stopped-appearing ⇒ probably sold.
Known limit: only refreshes for neighborhoods that keep getting hunted.

Two tiers (spec §8a):
  - STALE  (7 d):  excluded from every read path — feed, search, watchlist
                   listings, and §8b's sufficiency gate.
  - PURGE  (90 d): hard-deleted by the beat task. Deliberately far out — the
                   price-intelligence layer needs the history a short purge
                   would destroy.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

LISTING_STALE_DAYS = int(os.environ.get("LISTING_STALE_DAYS", "7"))
LISTING_PURGE_DAYS = int(os.environ.get("LISTING_PURGE_DAYS", "90"))

# System-owned accounts (house seeds) live on this domain. It receives no
# mail — deliveries to it bounce and damage sender reputation.
INTERNAL_EMAIL_DOMAIN = "studeal.internal"


def stale_cutoff(now: datetime | None = None) -> datetime:
    """Listings with last_seen_at older than this are invisible to reads."""
    return (now or datetime.now(timezone.utc)) - timedelta(days=LISTING_STALE_DAYS)


def purge_cutoff(now: datetime | None = None) -> datetime:
    """Listings with last_seen_at older than this are deleted."""
    return (now or datetime.now(timezone.utc)) - timedelta(days=LISTING_PURGE_DAYS)


def is_internal_user(email: str) -> bool:
    """True for system accounts that must never receive deliveries."""
    return email.lower().endswith(f"@{INTERNAL_EMAIL_DOMAIN}")
