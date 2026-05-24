"""add validation fields to deals (additive — expand step of expand-contract)

Adds the three columns that the validation layer will populate:
- legitimate: replaces score for "is this a real, surfacable deal?"
- validation_confidence: LLM confidence in the validation decision
- validation_reason: short human-readable reason (audit/debug)

Old columns (score, alert_tier, watchlists.alert_tier_threshold) are intentionally
preserved here and dropped in a separate migration once all code paths are migrated.

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-23
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deals",
        sa.Column(
            "legitimate",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "deals",
        sa.Column("validation_confidence", sa.Float(), nullable=True),
    )
    op.add_column(
        "deals",
        sa.Column("validation_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deals", "validation_reason")
    op.drop_column("deals", "validation_confidence")
    op.drop_column("deals", "legitimate")
