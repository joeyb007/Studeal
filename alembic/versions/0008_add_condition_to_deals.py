"""add condition to deals

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-19

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "deals",
        sa.Column("condition", sa.String(8), nullable=False, server_default="unknown"),
    )


def downgrade() -> None:
    op.drop_column("deals", "condition")
