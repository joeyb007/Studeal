from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from dealbot.api.limiter import limiter
from dealbot.api.routes.auth import router as auth_router
from dealbot.api.routes.billing import router as billing_router
from dealbot.api.routes.deals import router as deals_router
from dealbot.api.routes.watchlists import router as watchlists_router

app = FastAPI(title="DealBot API", version="0.1.0")

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
