"""create deals table

Revision ID: 0001
Revises:
Create Date: 2026-04-04

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("listed_price", sa.Float(), nullable=False),
        sa.Column("sale_price", sa.Float(), nullable=False),
        sa.Column("asin", sa.String(16), nullable=True),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("alert_tier", sa.String(16), nullable=False),
        sa.Column("category", sa.String(128), nullable=False),
        sa.Column("tags", sa.Text(), nullable=False),
        sa.Column("confidence", sa.String(8), nullable=False),
        sa.Column("real_discount_pct", sa.Float(), nullable=True),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url", name="uq_deals_url"),
    )


def downgrade() -> None:
    op.drop_table("deals")
