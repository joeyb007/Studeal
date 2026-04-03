from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dealbot.agents.scorer import ScorerAgent
from dealbot.graph.state import PipelineState
from dealbot.llm.base import LLMClient

logger = logging.getLogger(__name__)

DB_PATH = Path("deals.db")

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS deals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    source      TEXT NOT NULL,
    url         TEXT NOT NULL,
    listed_price REAL NOT NULL,
    sale_price  REAL NOT NULL,
    asin        TEXT,
    score       INTEGER NOT NULL,
    alert_tier  TEXT NOT NULL,
    category    TEXT NOT NULL,
    tags        TEXT NOT NULL,
    confidence  TEXT NOT NULL,
    real_discount_pct REAL,
    scraped_at  TEXT NOT NULL
)
"""


def _ensure_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(_CREATE_TABLE_SQL)
    conn.commit()
    return conn


# --- Nodes ------------------------------------------------------------------

async def ingest_node(state: PipelineState) -> PipelineState:
    """
    Validates the incoming DealRaw and passes it through.
    Also ensures the DB table exists so persist_node can write regardless of routing.
    In Phase 2 this will pull from a Redis stream instead.
    """
    logger.info("ingest_node: deal=%s source=%s", state["deal"].title, state["deal"].source)
    _ensure_db().close()
    return state


async def score_node(state: PipelineState, llm: LLMClient) -> PipelineState:
    """Runs ScorerAgent and writes the result into state."""
    deal = state["deal"]
    logger.info("score_node: scoring '%s'", deal.title)

    try:
        scorer = ScorerAgent(llm=llm)
        score_result = await scorer.score(deal)
        logger.info(
            "score_node: score=%d tier=%s confidence=%s",
            score_result.score,
            score_result.alert_tier,
            score_result.confidence,
        )
        return {**state, "score_result": score_result}
    except Exception as exc:
        logger.exception("score_node: failed to score deal '%s'", deal.title)
        return {**state, "error": str(exc)}


async def persist_node(state: PipelineState) -> PipelineState:
    """Writes DealScore to SQLite. Skipped silently if error is set."""
    if "error" in state:
        logger.warning("persist_node: skipping due to upstream error: %s", state["error"])
        return state

    score_result = state.get("score_result")
    if score_result is None:
        logger.warning("persist_node: no score_result in state, skipping")
        return state

    deal = score_result.deal
    conn = _ensure_db()
    try:
        conn.execute(
            """
            INSERT INTO deals (
                title, source, url, listed_price, sale_price, asin,
                score, alert_tier, category, tags, confidence,
                real_discount_pct, scraped_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                deal.title,
                deal.source,
                deal.url,
                deal.listed_price,
                deal.sale_price,
                deal.asin,
                score_result.score,
                score_result.alert_tier.value,
                score_result.category,
                json.dumps(score_result.tags),
                score_result.confidence,
                score_result.real_discount_pct,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        logger.info("persist_node: saved deal '%s' with score %d", deal.title, score_result.score)
    finally:
        conn.close()

    return state
