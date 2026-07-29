"""fleet and alerts tables

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hunts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("watchlist_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("offer_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("persisted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_listing_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["watchlist_id"], ["watchlists.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_hunts_watchlist_id", "hunts", ["watchlist_id"])

    op.create_table(
        "hunt_listings",
        sa.Column("hunt_id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("listing_id", sa.Integer(), nullable=False, primary_key=True),
        sa.Column("was_new_for_watchlist", sa.Boolean(), nullable=False, server_default="false"),
        sa.ForeignKeyConstraint(["hunt_id"], ["hunts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["listing_id"], ["listings.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "listing_alerts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("watchlist_id", sa.Integer(), nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("hunt_id", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("channels", sa.String(64), nullable=False, server_default="feed"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["watchlist_id"], ["watchlists.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["listing_id"], ["listings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["hunt_id"], ["hunts.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_listing_alerts_user_id", "listing_alerts", ["user_id"])

    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False, unique=True),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_push_subscriptions_user_id", "push_subscriptions", ["user_id"])

    op.add_column("watchlists", sa.Column("hunting_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")))
    op.add_column("watchlists", sa.Column("hunt_frequency_minutes", sa.Integer(), nullable=True))
    op.add_column("watchlists", sa.Column("last_hunt_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("watchlists", "last_hunt_at")
    op.drop_column("watchlists", "hunt_frequency_minutes")
    op.drop_column("watchlists", "hunting_enabled")

    op.drop_index("ix_push_subscriptions_user_id", table_name="push_subscriptions")
    op.drop_table("push_subscriptions")

    op.drop_index("ix_listing_alerts_user_id", table_name="listing_alerts")
    op.drop_table("listing_alerts")

    op.drop_table("hunt_listings")

    op.drop_index("ix_hunts_watchlist_id", table_name="hunts")
    op.drop_table("hunts")
