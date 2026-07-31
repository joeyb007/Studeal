"""Pool-metabolism policy: one module owns what "stale" and "purgeable" mean.

Staleness is measured on last_seen_at — the fleet re-hunts continuously and
bumps it on re-encounter, so a listing that stops being seen is probably sold.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dealbot.lifecycle import (
    LISTING_PURGE_DAYS,
    LISTING_STALE_DAYS,
    is_internal_user,
    purge_cutoff,
    stale_cutoff,
)


def test_defaults():
    assert LISTING_STALE_DAYS == 7
    assert LISTING_PURGE_DAYS == 90
    assert LISTING_STALE_DAYS < LISTING_PURGE_DAYS, (
        "retrieval staleness must precede deletion or reads serve purged rows"
    )


def test_cutoffs_are_relative_to_now():
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    assert stale_cutoff(now) == now - timedelta(days=LISTING_STALE_DAYS)
    assert purge_cutoff(now) == now - timedelta(days=LISTING_PURGE_DAYS)


def test_cutoffs_default_to_utcnow():
    before = datetime.now(timezone.utc) - timedelta(days=LISTING_STALE_DAYS)
    cut = stale_cutoff()
    after = datetime.now(timezone.utc) - timedelta(days=LISTING_STALE_DAYS)
    assert before <= cut <= after
    assert cut.tzinfo is not None, "naive cutoffs break tz-aware comparisons"


def test_internal_user_predicate():
    assert is_internal_user("house@studeal.internal") is True
    assert is_internal_user("HOUSE@STUDEAL.INTERNAL") is True
    assert is_internal_user("real.person@gmail.com") is False
    assert is_internal_user("attacker@studeal.internal.evil.com") is False
