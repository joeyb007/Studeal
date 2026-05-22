"""research-agent schema: drop watchlist_keywords, add intent_embedding, create hunt_queries

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop the old watchlist_keywords table (replaced by HuntQuery + intent_embedding)
    op.drop_table("watchlist_keywords")

    # 2. Add intent_embedding to watchlists
    op.add_column(
        "watchlists",
        sa.Column("intent_embedding", Vector(1536), nullable=True),
    )

    # 3. Create hunt_queries — every query the research agent issues
    op.create_table(
        "hunt_queries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "watchlist_id",
            sa.Integer(),
            sa.ForeignKey("watchlists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column(
            "hunt_timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
    )

    # 4. Many-to-many: which deals each hunt_query produced
    op.create_table(
        "hunt_query_deals",
        sa.Column(
            "hunt_query_id",
            sa.Integer(),
            sa.ForeignKey("hunt_queries.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "deal_id",
            sa.Integer(),
            sa.ForeignKey("deals.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    # 5. Helpful indexes
    op.create_index(
        "ix_hunt_queries_watchlist_id",
        "hunt_queries",
        ["watchlist_id"],
    )
    op.create_index(
        "ix_hunt_queries_hunt_timestamp",
        "hunt_queries",
        ["hunt_timestamp"],
    )


def downgrade() -> None:
    op.drop_index("ix_hunt_queries_hunt_timestamp", table_name="hunt_queries")
    op.drop_index("ix_hunt_queries_watchlist_id", table_name="hunt_queries")
    op.drop_table("hunt_query_deals")
    op.drop_table("hunt_queries")
    op.drop_column("watchlists", "intent_embedding")

    op.create_table(
        "watchlist_keywords",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "watchlist_id",
            sa.Integer(),
            sa.ForeignKey("watchlists.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("keyword", sa.String(128), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=True),
    )
