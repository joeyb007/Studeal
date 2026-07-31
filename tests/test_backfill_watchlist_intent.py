"""Existing watchlists hold vectors built from the bare product query. This
backfill re-embeds them from the full intent document so old and new rows are
drawn from the same distribution."""

from __future__ import annotations

import pytest

from dealbot.db.models import Watchlist
from dealbot.schemas import WatchlistContext


def test_skips_watchlists_without_context():
    from scripts.backfill_watchlist_intent import _needs_backfill

    no_ctx = Watchlist(user_id=1, name="empty", context=None)
    with_ctx = Watchlist(
        user_id=1, name="full",
        context=WatchlistContext(product_query="laptop").model_dump_json(),
    )
    assert _needs_backfill(no_ctx) is False, "no context → nothing to compose"
    assert _needs_backfill(with_ctx) is True


def test_skips_context_that_composes_to_blank():
    from scripts.backfill_watchlist_intent import _needs_backfill

    blank = Watchlist(
        user_id=1, name="blank",
        context=WatchlistContext(product_query="").model_dump_json(),
    )
    assert _needs_backfill(blank) is False, "blank document → embed_text no-ops anyway"


def test_malformed_context_json_does_not_raise():
    from scripts.backfill_watchlist_intent import _needs_backfill

    broken = Watchlist(user_id=1, name="broken", context="{not json")
    assert _needs_backfill(broken) is False, "one bad row must not abort the backfill"


def test_document_matches_the_create_path_composition():
    """The backfill and the API must embed byte-identical text, or backfilled
    vectors land in a different region than freshly-created ones."""
    from dealbot.recsys.intent import compose_intent_document
    from scripts.backfill_watchlist_intent import _document_for

    context = WatchlistContext(
        product_query="used laptop",
        max_budget=1200.0,
        brands=["Lenovo"],
        buyer_profile="CS student who commutes.",
    )
    row = Watchlist(user_id=1, name="w", context=context.model_dump_json())
    assert _document_for(row) == compose_intent_document(context)
