"""Bedrock migration: live vector columns 1536 → 1024 (Titan V2).

Vectors are DROPPED, not converted — OpenAI-space embeddings are meaningless
in Titan's space. Repopulate immediately after applying:

    scripts/backfill_listing_embeddings.py   (recreates the ivfflat index)
    scripts/backfill_watchlist_intent.py

hunt_queries.embedding changes type but has no live writers in v14 (dormant
since the legacy pipeline); nothing to repopulate. deals.embedding is legacy,
retiring, and deliberately untouched — its readers stay in their own 1536-d
space.

Dimensions are hardcoded, not derived from EMBED_DIM: this migration IS the
bedrock cutover and must mean the same thing regardless of the environment it
runs in.

Revision ID: 0025
Revises: 0024
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

_LIVE_COLUMNS = (
    ("listings", "embedding"),
    ("watchlists", "intent_embedding"),
    ("hunt_queries", "embedding"),
)


def upgrade() -> None:
    # The ivfflat index is bound to the old column type; the listings
    # backfill recreates it once vectors exist to train on.
    op.execute(sa.text("DROP INDEX IF EXISTS ix_listings_embedding"))
    for table, column in _LIVE_COLUMNS:
        op.execute(sa.text(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE vector(1024) USING NULL"
        ))


def downgrade() -> None:
    for table, column in _LIVE_COLUMNS:
        op.execute(sa.text(
            f"ALTER TABLE {table} ALTER COLUMN {column} "
            f"TYPE vector(1536) USING NULL"
        ))
