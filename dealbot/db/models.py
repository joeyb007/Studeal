from __future__ import annotations

from datetime import date, datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Deal(Base):
    __tablename__ = "deals"
    __table_args__ = (UniqueConstraint("url", name="uq_deals_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
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
    embedding: Mapped[list[float] | None] = mapped_column(Vector(768), nullable=True)
    hunt_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    scraped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    alerts: Mapped[list[Alert]] = relationship("Alert", back_populates="deal")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    hashed_password: Mapped[str] = mapped_column(Text, nullable=False)
    is_pro: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    watchlists: Mapped[list[Watchlist]] = relationship("Watchlist", back_populates="user")
    alerts: Mapped[list[Alert]] = relationship("Alert", back_populates="user")


class Watchlist(Base):
    __tablename__ = "watchlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    min_score: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    alert_tier_threshold: Mapped[str] = mapped_column(String(16), nullable=False, default="digest")

    user: Mapped[User] = relationship("User", back_populates="watchlists")
    keywords: Mapped[list[WatchlistKeyword]] = relationship(
        "WatchlistKeyword", back_populates="watchlist", cascade="all, delete-orphan"
    )
    alerts: Mapped[list[Alert]] = relationship("Alert", back_populates="watchlist")


class WatchlistKeyword(Base):
    __tablename__ = "watchlist_keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watchlist_id: Mapped[int] = mapped_column(
        ForeignKey("watchlists.id", ondelete="CASCADE"), nullable=False
    )
    keyword: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(768), nullable=True)

    watchlist: Mapped[Watchlist] = relationship("Watchlist", back_populates="keywords")


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (UniqueConstraint("user_id", "deal_id", name="uq_alerts_user_deal"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id", ondelete="CASCADE"), nullable=False)
    watchlist_id: Mapped[int] = mapped_column(
        ForeignKey("watchlists.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    user: Mapped[User] = relationship("User", back_populates="alerts")
    deal: Mapped[Deal] = relationship("Deal", back_populates="alerts")
    watchlist: Mapped[Watchlist] = relationship("Watchlist", back_populates="alerts")
