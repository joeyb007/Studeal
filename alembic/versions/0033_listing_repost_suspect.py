"""listings.repost_suspect: deterministic reused-listing trust signal.

Revision ID: 0033
Revises: 0032
"""

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "listings",
        sa.Column("repost_suspect", sa.Boolean, nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("listings", "repost_suspect")
