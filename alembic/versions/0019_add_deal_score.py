"""add deal_score to deals

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deals", sa.Column("deal_score", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("deals", "deal_score")
