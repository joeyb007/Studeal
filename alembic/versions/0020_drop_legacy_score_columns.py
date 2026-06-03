"""drop legacy score and alert_tier columns

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("deals", "score")
    op.drop_column("deals", "alert_tier")
    op.drop_column("watchlists", "alert_tier_threshold")


def downgrade() -> None:
    op.add_column("deals", sa.Column("score", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("deals", sa.Column("alert_tier", sa.String(16), nullable=False, server_default="digest"))
    op.add_column("watchlists", sa.Column("alert_tier_threshold", sa.String(16), nullable=False, server_default="digest"))
