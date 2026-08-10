"""inspection_checklists.purchase_context: thread-local tailoring answer.

Revision ID: 0036
Revises: 0035
"""

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("inspection_checklists", sa.Column("purchase_context", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("inspection_checklists", "purchase_context")
