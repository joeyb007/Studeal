"""watchlist_rankings — the precomputed recsys cache.

The listings read path previously ran retrieval + listwise LLM ranking per
request (3-8s). Rankings are now precomputed on events (hunt completion,
context edit; lazy staleness backstop) and served from these rows.

Revision ID: 0026
Revises: 0025
"""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watchlist_rankings",
        sa.Column("watchlist_id", sa.Integer,
                  sa.ForeignKey("watchlists.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("listing_id", sa.Integer,
                  sa.ForeignKey("listings.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_watchlist_rankings_wl_pos", "watchlist_rankings",
        ["watchlist_id", "position"],
    )


def downgrade() -> None:
    op.drop_index("ix_watchlist_rankings_wl_pos")
    op.drop_table("watchlist_rankings")
