"""watchlist playbook columns — Scout's category expertise per watchlist.

Revision ID: 0028
Revises: 0027
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("watchlists", sa.Column("playbook", sa.Text(), nullable=True))
    op.add_column(
        "watchlists",
        sa.Column("playbook_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("watchlists", "playbook_updated_at")
    op.drop_column("watchlists", "playbook")
