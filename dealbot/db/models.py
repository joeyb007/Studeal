from __future__ import annotations

from datetime import date, datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Table, Text, UniqueConstraint, Column
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Deal(Base):
    __tablename__ = "deals"
    __table_args__ = (UniqueConstraint("url", name="uq_deals_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    listed_price: Mapped[float] = mapped_column(Float, nullable=False)
    sale_price: Mapped[float] = mapped_column(Float, nullable=False)
    asin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    alert_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    tags: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    real_discount_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    student_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    condition: Mapped[str] = mapped_column(String(8), nullable=False, default="unknown")
    affiliate_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    legitimate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    validation_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    validation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    deal_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hunt_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    hashed_password: Mapped[str] = mapped_column(Text, nullable=False)
    is_pro: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    google_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    secondhand_searches_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    watchlists: Mapped[list[Watchlist]] = relationship("Watchlist", back_populates="user")


hunt_query_deals = Table(
    "hunt_query_deals",
    Base.metadata,
    Column("hunt_query_id", ForeignKey("hunt_queries.id", ondelete="CASCADE"), primary_key=True),
    Column("deal_id", ForeignKey("deals.id", ondelete="CASCADE"), primary_key=True),
)


class Watchlist(Base):
    __tablename__ = "watchlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    min_score: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    alert_tier_threshold: Mapped[str] = mapped_column(String(16), nullable=False, default="digest")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    intent_embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)

    user: Mapped[User] = relationship("User", back_populates="watchlists")
    hunt_queries: Mapped[list[HuntQuery]] = relationship(
        "HuntQuery", back_populates="watchlist", cascade="all, delete-orphan"
    )


class HuntQuery(Base):
    """A query issued by the research agent. Used for semantic dedup (Layer 1)
    and for cheap daily cron rehunt without re-running the full ReAct loop."""

    __tablename__ = "hunt_queries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watchlist_id: Mapped[int] = mapped_column(
        ForeignKey("watchlists.id", ondelete="CASCADE"), nullable=False
    )
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    hunt_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    watchlist: Mapped[Watchlist] = relationship("Watchlist", back_populates="hunt_queries")
    deals: Mapped[list[Deal]] = relationship("Deal", secondary=hunt_query_deals)
