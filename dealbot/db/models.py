from __future__ import annotations

from datetime import date, datetime, timezone

from pgvector.sqlalchemy import Vector

from dealbot.llm.embeddings import EMBED_DIM
from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Table, Text, UniqueConstraint, Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Listing(Base):
    """marketplace listing — extracted by the Explorer/Extractor pipeline,
    persisted with a per-marketplace canonicalized URL as the dedup key."""

    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("canonical_url", name="uq_listings_canonical_url"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    raw_url: Mapped[str] = mapped_column(Text, nullable=False)
    marketplace: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(String(256), nullable=True)
    posted_at_raw: Mapped[str | None] = mapped_column(String(64), nullable=True)
    condition: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    # Semantic search over the pool (Daily Drops). Nullable: embedding is an
    # enhancement, never a precondition for persisting a listing.
    # Dimension follows EMBEDDING_BACKEND (migration 0025).
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    # Set when an inspection lands on a dead page: read surfaces exclude sold
    # rows immediately instead of waiting out the staleness window.
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Trust signal (spec 2026-08-10): same title elsewhere at a different
    # location or materially different price. Badge-only; never hides a row.
    repost_suspect: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


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
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    tags: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array
    confidence: Mapped[str] = mapped_column(String(8), nullable=False)
    real_discount_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    student_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    condition: Mapped[str] = mapped_column(String(8), nullable=False, default="unknown")
    affiliate_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Legacy OpenAI space, retiring with the deals pipeline — NOT migrated.
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
    # Free-tier inspection allowance: fresh Tier A runs this calendar month.
    # Cached-report reads never count; Pro is uncapped.
    inspections_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inspections_month: Mapped[str | None] = mapped_column(String(7), nullable=True)
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
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    intent_embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM), nullable=True)
    hunting_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    hunt_frequency_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_hunt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Scout's category playbook (Layer 1 of the Deal Inspector): generated on
    # creation and profile edits, alongside the ranking recompute.
    playbook: Mapped[str | None] = mapped_column(Text, nullable=True)
    playbook_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM), nullable=True)
    hunt_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    watchlist: Mapped[Watchlist] = relationship("Watchlist", back_populates="hunt_queries")
    deals: Mapped[list[Deal]] = relationship("Deal", secondary=hunt_query_deals)


class Hunt(Base):
    __tablename__ = "hunts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watchlist_id: Mapped[int] = mapped_column(
        ForeignKey("watchlists.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    offer_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    persisted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_listing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class HuntListing(Base):
    __tablename__ = "hunt_listings"

    hunt_id: Mapped[int] = mapped_column(
        ForeignKey("hunts.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, primary_key=True
    )
    # How this listing joined the hunt: "browsed" (found live by this hunt's
    # own browsing) or "pool" (served from another agent's earlier find).
    # The %-of-alerts-from-pool metric is the pool thesis as a measurement.
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="browsed"
    )
    was_new_for_watchlist: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ListingAlert(Base):
    __tablename__ = "listing_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    watchlist_id: Mapped[int] = mapped_column(
        ForeignKey("watchlists.id", ondelete="CASCADE"), nullable=False
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), nullable=False
    )
    hunt_id: Mapped[int] = mapped_column(
        ForeignKey("hunts.id", ondelete="CASCADE"), nullable=False
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    channels: Mapped[str] = mapped_column(String(64), nullable=False, default="feed")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HuntLane(Base):
    """Per-(query, marketplace) lane state for one hunt — the DB half of the
    Mission Control contract: pub/sub has no replay, so the UI seeds from
    these rows on load and treats the SSE stream as live augmentation.
    Screenshots are deliberately not persisted (heavy, refresh within
    seconds of reconnect)."""

    __tablename__ = "hunt_lanes"

    hunt_id: Mapped[int] = mapped_column(
        ForeignKey("hunts.id", ondelete="CASCADE"), primary_key=True
    )
    query: Mapped[str] = mapped_column(String(256), primary_key=True)
    marketplace: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    pages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    done_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class WatchlistRanking(Base):
    """Precomputed recsys output: the full ordered ranking of pool listings
    for one watchlist. The read path serves these rows (~50ms); recomputes
    are event-driven (hunt completion, context edit) with a lazy staleness
    backstop — the LLM never runs on a request path."""

    __tablename__ = "watchlist_rankings"

    watchlist_id: Mapped[int] = mapped_column(
        ForeignKey("watchlists.id", ondelete="CASCADE"), primary_key=True
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True
    )
    score: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class ListingInspection(Base):
    """Scout's cached objective look at one listing (Deal Inspector Tier A).

    One row per listing, shared across users: the first inspector pays the
    browser visit + vision call, everyone else reads. The personal verdict
    (Tier B) is computed per watchlist on demand and never cached here.
    """

    __tablename__ = "listing_inspections"

    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")  # ok | listing_gone
    report: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON, sanitized
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON ListingDetail
    # Objective flags distilled from the report; shared across users, so
    # nothing user-specific lives here (personal matching is rank-time work).
    # JSONB on postgres; plain JSON keeps the sqlite test harness compiling.
    flags: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class InspectionWatch(Base):
    """Price-drop watch created when a user sends a listing to Scout.

    Inspecting is the strongest interest signal in the product; if the
    listing's price later drops below what it was at inspection time, the
    user gets one email and the row is marked notified.
    """

    __tablename__ = "inspection_watches"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), primary_key=True
    )
    price_at_inspection: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InspectionMessage(Base):
    """Persisted send-to-Scout conversation turn. A friend remembers what
    you talked about: reopening a listing rehydrates the whole thread."""

    __tablename__ = "inspection_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(12), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
