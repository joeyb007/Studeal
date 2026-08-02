"""Typed hunt events — the write side of the Mission Control SSE contract.

Every event serializes to a flat JSON envelope: v, type, ts, hunt_id,
watchlist_id + type-specific fields. Events are published to Redis pub/sub
channel `events:watchlist:{watchlist_id}` and forwarded verbatim over SSE.
Pub/sub has no replay — consumers render DB state on load and treat the
stream as live augmentation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _EventBase(BaseModel):
    v: int = 1
    ts: datetime = Field(default_factory=_now)
    hunt_id: int
    watchlist_id: int


class HuntStarted(_EventBase):
    type: Literal["hunt.started"] = "hunt.started"


class QueriesPlanned(_EventBase):
    type: Literal["hunt.queries_planned"] = "hunt.queries_planned"
    queries: list[str]


class ExplorerTurn(_EventBase):
    type: Literal["explorer.turn"] = "explorer.turn"
    query: str
    marketplace: str
    turn: int
    url: str
    action: str
    result: str


class ExplorerScreenshot(_EventBase):
    type: Literal["explorer.screenshot"] = "explorer.screenshot"
    query: str
    marketplace: str
    turn: int
    image_data_url: str


class ExplorerError(_EventBase):
    type: Literal["explorer.error"] = "explorer.error"
    query: str
    marketplace: str
    error: str


class ExtractionSubmitted(_EventBase):
    type: Literal["extraction.submitted"] = "extraction.submitted"
    query: str
    marketplace: str


class HuntPersisted(_EventBase):
    type: Literal["hunt.persisted"] = "hunt.persisted"
    offer_count: int
    persisted_count: int
    new_for_watchlist: int


class LanesPlanned(_EventBase):
    """Published per query right after routing: the full set of lanes this
    query will run. Mission Control renders them as queued tiles immediately,
    so progress is legible (N of M complete) even while the concurrency cap
    staggers actual browsing."""

    type: Literal["lanes.planned"] = "lanes.planned"
    query: str
    marketplaces: list[str]


class LaneFinished(_EventBase):
    """One (query, marketplace) browse lane completed — others may still
    be running. Mission Control flips that lane's tile to its done state."""

    type: Literal["lane.finished"] = "lane.finished"
    query: str
    marketplace: str
    pages: int
    done_reason: str


class HuntFinished(_EventBase):
    type: Literal["hunt.finished"] = "hunt.finished"
    status: Literal["succeeded", "failed", "cached"]
    duration_s: float
    error: str | None = None


class AlertCreated(_EventBase):
    type: Literal["alert.created"] = "alert.created"
    alert_id: int
    listing_id: int
    title: str
    price: float
    currency: str
    score: float
    url: str


Event = Annotated[
    Union[
        HuntStarted,
        QueriesPlanned,
        ExplorerTurn,
        ExplorerScreenshot,
        ExplorerError,
        ExtractionSubmitted,
        HuntPersisted,
        LanesPlanned,
        LaneFinished,
        HuntFinished,
        AlertCreated,
    ],
    Field(discriminator="type"),
]


def channel_for(watchlist_id: int) -> str:
    return f"events:watchlist:{watchlist_id}"
