"""The intent document — the text whose embedding is the user's preference vector.

In classical content-based filtering the user parameter vector is fitted to past
ratings. Here it is *elicited*: Scout converses, fills a WatchlistContext, and
this module renders that context into the string we embed. That sidesteps the
cold-start problem entirely — a brand-new user has a meaningful vector on signup.

Composition is deliberately prose-flavoured rather than key-value: the embedding
model was trained on natural language, and "budget around $1200" sits closer to
listing text than "max_budget=1200.0" does.
"""

from __future__ import annotations

from dealbot.schemas import WatchlistContext


def compose_intent_document(context: WatchlistContext) -> str:
    """Render a watchlist context into embeddable text.

    Deterministic and total: every field is optional, and absent fields are
    omitted rather than stringified. Callers must use this rather than composing
    their own text, so that freshly-created and backfilled vectors are drawn
    from the same distribution.
    """
    parts: list[str] = []

    if context.product_query.strip():
        parts.append(context.product_query.strip())
    if context.buyer_profile and context.buyer_profile.strip():
        parts.append(context.buyer_profile.strip())
    if context.appearance_notes and context.appearance_notes.strip():
        # The physical brief pulls physically-matching titles up in retrieval.
        parts.append(f"Looks: {context.appearance_notes.strip()}.")
    if context.brands:
        parts.append(f"Prefers brands: {', '.join(context.brands)}.")
    if context.condition:
        parts.append(f"Open to condition: {', '.join(context.condition)}.")
    if context.max_budget is not None:
        # Prose, not a constraint. The hard budget ceiling is a SQL filter; this
        # only nudges retrieval toward the right price tier.
        parts.append(f"Budget around ${context.max_budget:.0f}.")

    return " ".join(parts)
