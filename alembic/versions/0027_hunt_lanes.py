"""hunt_lanes — persisted lane state so Mission Control survives refresh.

Revision ID: 0027
Revises: 0026
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hunt_lanes",
        sa.Column("hunt_id", sa.Integer,
                  sa.ForeignKey("hunts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("query", sa.String(256), primary_key=True),
        sa.Column("marketplace", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("pages", sa.Integer, nullable=False, server_default="0"),
        sa.Column("done_reason", sa.Text, nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("hunt_lanes")
