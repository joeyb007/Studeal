"""inspection_messages.images: attached screenshot media keys.

Revision ID: 0035
Revises: 0034
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("inspection_messages", sa.Column("images", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("inspection_messages", "images")
