from __future__ import annotations

import os
from contextlib import asynccontextmanager

import logging

import sentry_sdk
from fastapi import FastAPI
from sentry_sdk.integrations.logging import LoggingIntegration
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from dealbot.api.limiter import limiter
from dealbot.api.routes.alerts import router as alerts_router
from dealbot.api.routes.auth import router as auth_router
from dealbot.api.routes.billing import router as billing_router
from dealbot.api.routes.inspections import router as inspections_router
from dealbot.api.routes.market import router as market_router
from dealbot.api.routes.media import router as media_router
from dealbot.api.routes.deals import router as deals_router
from dealbot.api.routes.email_prefs import router as email_prefs_router
from dealbot.api.routes.hunts import router as hunts_router
from dealbot.api.routes.listings_feed import router as listings_feed_router
from dealbot.api.routes.push import router as push_router
from dealbot.api.routes.stream import router as stream_router
from dealbot.api.routes.watchlists import router as watchlists_router
from dealbot.config import validate_env

_raw_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000")
_ALLOWED_ORIGINS: list[str] = [o.strip() for o in _raw_origins.split(",") if o.strip()]


_SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if _SENTRY_DSN:
    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        traces_sample_rate=0.1,
        send_default_pii=False,
        integrations=[
            LoggingIntegration(
                level=logging.WARNING,
                event_level=logging.ERROR,
            )
        ],
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_env()
    yield


app = FastAPI(title="DealBot API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.include_router(alerts_router)
app.include_router(auth_router)
app.include_router(billing_router)
app.include_router(deals_router)
app.include_router(email_prefs_router)
app.include_router(hunts_router)
app.include_router(inspections_router)
app.include_router(listings_feed_router)
app.include_router(market_router)
app.include_router(media_router)
app.include_router(push_router)
app.include_router(stream_router)
app.include_router(watchlists_router)


@app.get("/health/spend")
async def health_spend() -> dict:
    """Today's spend ledgers vs budgets — the runaway-cost dashboard."""
    from dealbot.costs import (
        BROWSERBASE_MONTHLY_SESSION_CAP,
        DAILY_BROWSER_SESSION_CAP,
        DAILY_LLM_BUDGET_USD,
        PROXY_DAILY_SESSION_CAP,
        PROXY_MONTHLY_SESSION_CAP,
        build_meter,
        fleet_paused,
    )

    meter = build_meter()
    try:
        llm = round(await meter.llm_spend_today(), 4)
        sessions = await meter.sessions_today()
        bb_month = await meter.bb_sessions_month()
        proxy_day = await meter.proxy_sessions_today()
        proxy_month = await meter.proxy_sessions_month()
        by_stage = await meter.llm_by_stage()
    except Exception:
        llm, sessions, bb_month, proxy_day, proxy_month = None, None, None, None, None
        by_stage = None
    return {
        "llm_spend_usd": llm,
        "llm_budget_usd": DAILY_LLM_BUDGET_USD,
        # stage:tier -> $ today, biggest first. Answers "what do we cut?"
        "llm_by_stage": by_stage,
        "browser_sessions": sessions,
        "browser_session_cap": DAILY_BROWSER_SESSION_CAP,
        "browserbase_sessions_month": bb_month,
        "browserbase_month_cap": BROWSERBASE_MONTHLY_SESSION_CAP,
        "proxy_sessions_today": proxy_day,
        "proxy_daily_cap": PROXY_DAILY_SESSION_CAP,
        "proxy_sessions_month": proxy_month,
        "proxy_monthly_cap": PROXY_MONTHLY_SESSION_CAP,
        "fleet_paused": fleet_paused(),
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
