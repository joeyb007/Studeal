"""add hunt_date to deals

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-13

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("deals", sa.Column("hunt_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("deals", "hunt_date")
