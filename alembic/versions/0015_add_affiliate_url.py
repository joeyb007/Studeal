"""add affiliate_url to deals

Revision ID: 0015
Revises: 0014
Create Date: 2026-04-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deals", sa.Column("affiliate_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("deals", "affiliate_url")
