"""add embedding to deals

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-06

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBED_DIM = 768


def upgrade() -> None:
    # pgvector extension is a DB-level prerequisite — install it before running migrations:
    #   docker exec <postgres-container> psql -U postgres -d dealbot -c "CREATE EXTENSION IF NOT EXISTS vector;"
    op.execute(sa.text(f"ALTER TABLE deals ADD COLUMN IF NOT EXISTS embedding vector({EMBED_DIM})"))


def downgrade() -> None:
    op.drop_column("deals", "embedding")
    # Intentionally leave the vector extension in place — other tables may use it
