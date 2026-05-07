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
from dealbot.api.routes.auth import router as auth_router
from dealbot.api.routes.billing import router as billing_router
from dealbot.api.routes.deals import router as deals_router
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

app.include_router(auth_router)
app.include_router(billing_router)
app.include_router(deals_router)
app.include_router(watchlists_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
