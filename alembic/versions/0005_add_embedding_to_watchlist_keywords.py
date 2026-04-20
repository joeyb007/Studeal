"""add embedding to watchlist_keywords

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-06

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBED_DIM = 768


def upgrade() -> None:
    # vector extension already enabled by 0004
    op.execute(sa.text(f"ALTER TABLE watchlist_keywords ADD COLUMN IF NOT EXISTS embedding vector({EMBED_DIM})"))


def downgrade() -> None:
    op.drop_column("watchlist_keywords", "embedding")
