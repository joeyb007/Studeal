"""add embedding to listings

Powers semantic search over the shared agent pool (Daily Drops). Mirrors the
`deals.embedding` setup from 0004 — raw SQL, since pgvector column types need
the extension present.

The ivfflat index is NOT created here: it needs rows to train on, and a fresh
database has none. `scripts/backfill_listing_embeddings.py` creates it after
populating vectors.

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa
from alembic import op

from dealbot.llm.embeddings import EMBED_DIM

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    op.execute(sa.text(
        f"ALTER TABLE listings ADD COLUMN IF NOT EXISTS embedding vector({EMBED_DIM})"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_listings_embedding"))
    op.drop_column("listings", "embedding")
