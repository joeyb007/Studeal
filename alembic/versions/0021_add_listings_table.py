"""add listings table

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "listings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("raw_url", sa.Text(), nullable=False),
        sa.Column("marketplace", sa.String(64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("location", sa.String(256), nullable=True),
        sa.Column("posted_at_raw", sa.String(64), nullable=True),
        sa.Column(
            "condition", sa.String(16), nullable=False, server_default="unknown",
        ),
        sa.Column(
            "first_seen_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.UniqueConstraint("canonical_url", name="uq_listings_canonical_url"),
    )
    op.create_index(
        "ix_listings_marketplace", "listings", ["marketplace"],
    )


def downgrade() -> None:
    op.drop_index("ix_listings_marketplace", table_name="listings")
    op.drop_table("listings")
