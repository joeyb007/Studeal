"""inspection_checklists: per-user ready-to-buy evidence state.

Revision ID: 0034
Revises: 0033
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inspection_checklists",
        sa.Column("user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("listing_id", sa.Integer,
                  sa.ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("watchlist_id", sa.Integer,
                  sa.ForeignKey("watchlists.id", ondelete="SET NULL"), nullable=True),
        sa.Column("items", JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("inspection_checklists")
