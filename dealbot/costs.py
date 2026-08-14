"""Daily spend metering + budget guards (2026-08-12 pre-ship spec).

Two ledgers in Redis, keyed by UTC day (expire after 2 days):
  spend:llm:<YYYY-MM-DD>       — accumulated $ across every Bedrock call
  spend:sessions:<YYYY-MM-DD>  — Browserbase sessions created

Guards:
  - Background fleet work (hunts, auto-inspects, embed sweeps) stops at
    100% of DAILY_LLM_BUDGET_USD.
  - Interactive, user-triggered paths (chat, link fetch) keep working up
    to INTERACTIVE_BUDGET_FACTOR × budget, then 503 honestly.
  - DAILY_BROWSER_SESSION_CAP bounds session creations outright.
  - FLEET_PAUSED=1 is the manual break-glass: no hunts dispatch, period.

Failure direction: every guard fails OPEN on Redis errors (a metering blip
must never take the product down) but logs loudly — same policy as the
fleet governor. Costs are estimates from published on-demand Bedrock
pricing; they exist to catch runaways, not to reconcile invoices.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

DAILY_LLM_BUDGET_USD = float(os.environ.get("DAILY_LLM_BUDGET_USD", "25"))
DAILY_BROWSER_SESSION_CAP = int(os.environ.get("DAILY_BROWSER_SESSION_CAP", "300"))
# Browserbase-only, MONTHLY, sized to the plan's included quota (100 h / 1 GB):
# 500 sessions × ~6 min × ~2 MB (media-blocked) stays inside both meters, so
# the bill cannot exceed the flat plan price. Past the cap those lanes fail
# honestly; agentcore lanes are unaffected.
BROWSERBASE_MONTHLY_SESSION_CAP = int(os.environ.get("BROWSERBASE_MONTHLY_SESSION_CAP", "500"))
INTERACTIVE_BUDGET_FACTOR = 1.5

_LEDGER_TTL_S = 2 * 24 * 3600
_MONTH_LEDGER_TTL_S = 45 * 24 * 3600

# $ per 1M tokens (input, output) — on-demand us-east-1, checked 2026-08.
# Unknown models fall back to the frontier rate: over-counting an unknown
# model fails toward stopping sooner, which is the safe direction.
_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "sonnet": (3.00, 15.00),
    "haiku": (1.00, 5.00),
    "titan-embed": (0.80, 0.0),      # embeddings bill input-only
}
_FALLBACK_PRICE = (3.00, 15.00)


def price_for_model(model_id: str) -> tuple[float, float]:
    lowered = (model_id or "").lower()
    for token, price in _PRICES_PER_MTOK.items():
        if token in lowered:
            return price
    return _FALLBACK_PRICE


def estimate_call_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Dollars for one call. Pure; unit-tested."""
    in_rate, out_rate = price_for_model(model_id)
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


def _day_key(prefix: str) -> str:
    return f"{prefix}:{datetime.now(timezone.utc):%Y-%m-%d}"


def _month_key(prefix: str) -> str:
    return f"{prefix}:{datetime.now(timezone.utc):%Y-%m}"


def fleet_paused() -> bool:
    return os.environ.get("FLEET_PAUSED", "").lower() in ("1", "true", "yes")


class SpendMeter:
    """Fire-and-forget recording, fail-open checks."""

    def __init__(self, client: "aioredis.Redis") -> None:
        self._client = client

    async def record_llm(self, model_id: str, input_tokens: int, output_tokens: int) -> None:
        cost = estimate_call_cost(model_id, input_tokens, output_tokens)
        if cost <= 0:
            return
        try:
            key = _day_key("spend:llm")
            await self._client.incrbyfloat(key, cost)
            await self._client.expire(key, _LEDGER_TTL_S)
        except Exception:
            logger.warning("spend meter: llm record failed", exc_info=True)

    async def record_session(self) -> None:
        try:
            key = _day_key("spend:sessions")
            await self._client.incr(key)
            await self._client.expire(key, _LEDGER_TTL_S)
        except Exception:
            logger.warning("spend meter: session record failed", exc_info=True)

    async def llm_spend_today(self) -> float:
        raw = await self._client.get(_day_key("spend:llm"))
        if raw is None:
            return 0.0
        return float(raw.decode() if isinstance(raw, bytes) else raw)

    async def sessions_today(self) -> int:
        raw = await self._client.get(_day_key("spend:sessions"))
        if raw is None:
            return 0
        return int(raw.decode() if isinstance(raw, bytes) else raw)

    async def llm_budget_ok(self, *, factor: float = 1.0) -> bool:
        """True while spend is under factor × budget. Fails OPEN."""
        try:
            return (await self.llm_spend_today()) < DAILY_LLM_BUDGET_USD * factor
        except Exception:
            logger.warning("spend meter: budget check failed — failing open", exc_info=True)
            return True

    async def session_cap_ok(self) -> bool:
        try:
            return (await self.sessions_today()) < DAILY_BROWSER_SESSION_CAP
        except Exception:
            logger.warning("spend meter: session check failed — failing open", exc_info=True)
            return True

    async def record_bb_session(self) -> None:
        try:
            key = _month_key("spend:bb_sessions")
            await self._client.incr(key)
            await self._client.expire(key, _MONTH_LEDGER_TTL_S)
        except Exception:
            logger.warning("spend meter: bb session record failed", exc_info=True)

    async def bb_sessions_month(self) -> int:
        raw = await self._client.get(_month_key("spend:bb_sessions"))
        if raw is None:
            return 0
        return int(raw.decode() if isinstance(raw, bytes) else raw)

    async def bb_month_cap_ok(self) -> bool:
        """The only real-dollar meter left: browserbase sessions this month.
        Fails OPEN like every guard — if Redis is down, hunts are broken
        anyway (it's also the celery broker)."""
        try:
            return (await self.bb_sessions_month()) < BROWSERBASE_MONTHLY_SESSION_CAP
        except Exception:
            logger.warning("spend meter: bb month check failed — failing open", exc_info=True)
            return True


_meter_cache: dict[str, object] = {"loop": None, "meter": None}


def build_meter() -> SpendMeter:
    """Per-event-loop meter over $REDIS_URL (same pattern as the governor)."""
    loop = asyncio.get_running_loop()
    if _meter_cache["loop"] is not loop:
        _meter_cache["loop"] = loop
        url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        _meter_cache["meter"] = SpendMeter(aioredis.from_url(url))
    return _meter_cache["meter"]  # type: ignore[return-value]


async def require_interactive_budget() -> None:
    """FastAPI dependency for user-triggered LLM endpoints (chat, link
    fetch): keeps working past the fleet cutoff, stops honestly at
    INTERACTIVE_BUDGET_FACTOR × budget."""
    from fastapi import HTTPException

    if not await build_meter().llm_budget_ok(factor=INTERACTIVE_BUDGET_FACTOR):
        raise HTTPException(
            status_code=503,
            detail="Studeal hit its daily compute budget. Back tomorrow.",
        )


def record_llm_nowait(model_id: str, input_tokens: int, output_tokens: int) -> None:
    """Schedule a spend record without awaiting it — LLM calls must never
    block on metering. No-op outside a running event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(build_meter().record_llm(model_id, input_tokens, output_tokens))
