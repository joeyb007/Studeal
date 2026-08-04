"""inspection_watches + free-tier inspection allowance columns.

Revision ID: 0030
Revises: 0029
"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inspection_watches",
        sa.Column("user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("listing_id", sa.Integer,
                  sa.ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("price_at_inspection", sa.Float, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("inspections_used", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column(
        "users",
        sa.Column("inspections_month", sa.String(7), nullable=True),  # "2026-08"
    )


def downgrade() -> None:
    op.drop_column("users", "inspections_month")
    op.drop_column("users", "inspections_used")
    op.drop_table("inspection_watches")
