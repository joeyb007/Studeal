"""The intent document is the text whose embedding *is* the user's preference
vector. Composition must be deterministic and identical across the create path
and the backfill script, or the two produce vectors in different regions."""

from __future__ import annotations

from dealbot.recsys.intent import compose_intent_document
from dealbot.schemas import WatchlistContext


def test_bare_query_composes_to_the_query():
    doc = compose_intent_document(WatchlistContext(product_query="used laptop"))
    assert "used laptop" in doc


def test_profile_and_attributes_are_included():
    doc = compose_intent_document(WatchlistContext(
        product_query="used laptop",
        max_budget=1200.0,
        condition=["used", "refurb"],
        brands=["Lenovo", "Dell"],
        buyer_profile="CS student who codes on the train; values battery life.",
    ))
    assert "used laptop" in doc
    assert "CS student who codes on the train" in doc
    assert "Lenovo" in doc and "Dell" in doc
    assert "used" in doc and "refurb" in doc


def test_absent_fields_leave_no_dangling_labels():
    doc = compose_intent_document(WatchlistContext(product_query="bike"))
    assert "None" not in doc, "a null field must be omitted, not stringified"
    assert "brands:" not in doc.lower()


def test_is_deterministic():
    ctx = WatchlistContext(
        product_query="monitor", brands=["Dell"], buyer_profile="Designer.",
    )
    assert compose_intent_document(ctx) == compose_intent_document(ctx)


def test_empty_query_still_composes_from_profile():
    """A watchlist can exist with an empty product_query (see the patch route's
    fallback). The profile alone must still yield embeddable text."""
    doc = compose_intent_document(WatchlistContext(
        product_query="", buyer_profile="Student furnishing a first apartment.",
    ))
    assert doc.strip(), "must not return blank — embed_text() no-ops on blank input"
    assert "first apartment" in doc
