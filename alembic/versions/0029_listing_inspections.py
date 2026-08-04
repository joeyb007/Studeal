"""listing_inspections cache + listings.sold_at (Deal Inspector Tier A).

Revision ID: 0029
Revises: 0028
"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "listings",
        sa.Column("sold_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "listing_inspections",
        sa.Column("listing_id", sa.Integer,
                  sa.ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
        sa.Column("report", sa.Text, nullable=True),
        sa.Column("detail", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("listing_inspections")
    op.drop_column("listings", "sold_at")
