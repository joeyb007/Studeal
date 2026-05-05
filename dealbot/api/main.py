from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from dealbot.api.limiter import limiter
from dealbot.api.routes.auth import router as auth_router
from dealbot.api.routes.billing import router as billing_router
from dealbot.api.routes.deals import router as deals_router
from dealbot.api.routes.watchlists import router as watchlists_router

_raw_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000")
_ALLOWED_ORIGINS: list[str] = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app = FastAPI(title="DealBot API", version="0.1.0")

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
