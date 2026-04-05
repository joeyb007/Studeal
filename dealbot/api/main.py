from __future__ import annotations

from fastapi import FastAPI

from dealbot.api.routes.deals import router as deals_router

app = FastAPI(title="DealBot API", version="0.1.0")

app.include_router(deals_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
