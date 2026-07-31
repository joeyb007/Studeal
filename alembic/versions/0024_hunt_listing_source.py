"""hunt_listings.source — browsed vs pool provenance.

Server default keeps every pre-existing row (and every writer that does not
set the column) meaning "browsed", which is what they all were.

Revision ID: 0024
Revises: 0023
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "hunt_listings",
        sa.Column("source", sa.String(16), nullable=False, server_default="browsed"),
    )


def downgrade() -> None:
    op.drop_column("hunt_listings", "source")
