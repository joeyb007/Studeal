"""url nullable, embedding dim 768->1536

Revision ID: 0014
Revises: 0013
Create Date: 2026-04-26
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Make deals.url nullable
    op.alter_column("deals", "url", existing_type=sa.Text(), nullable=True)

    # Change embedding columns from vector(768) to vector(1536)
    # Must drop and recreate — pgvector doesn't support ALTER COLUMN for vector dims
    op.execute("ALTER TABLE deals DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE deals ADD COLUMN embedding vector(1536)")

    op.execute("ALTER TABLE watchlist_keywords DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE watchlist_keywords ADD COLUMN embedding vector(1536)")


def downgrade() -> None:
    op.alter_column("deals", "url", existing_type=sa.Text(), nullable=False)

    op.execute("ALTER TABLE deals DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE deals ADD COLUMN embedding vector(768)")

    op.execute("ALTER TABLE watchlist_keywords DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE watchlist_keywords ADD COLUMN embedding vector(768)")
