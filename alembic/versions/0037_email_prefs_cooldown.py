"""Per-type email preferences + alert-email cooldown anchor.

Revision ID: 0037
Revises: 0036
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("email_alerts", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "users",
        sa.Column("email_price_drops", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "users",
        sa.Column("email_digest", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "watchlists",
        sa.Column("last_alert_email_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("watchlists", "last_alert_email_at")
    op.drop_column("users", "email_digest")
    op.drop_column("users", "email_price_drops")
    op.drop_column("users", "email_alerts")
