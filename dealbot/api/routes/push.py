"""Push subscription routes — the browser registers/removes its web-push
endpoint here; the sender in dealbot.notifications.push consumes the rows."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from dealbot.api.auth import get_current_user
from dealbot.db.database import get_async_session
from dealbot.db.models import PushSubscription, User

router = APIRouter(prefix="/push", tags=["push"])


class PushKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscribeRequest(BaseModel):
    endpoint: str
    keys: PushKeys


class PushUnsubscribeRequest(BaseModel):
    endpoint: str


@router.get("/vapid-public-key")
async def vapid_public_key() -> dict[str, str]:
    key = os.environ.get("VAPID_PUBLIC_KEY", "")
    if not key:
        raise HTTPException(status_code=503, detail="Push notifications not configured")
    return {"key": key}


@router.post("/subscribe", status_code=status.HTTP_201_CREATED)
async def subscribe(
    body: PushSubscribeRequest,
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    async with get_async_session() as session:
        stmt = (
            pg_insert(PushSubscription)
            .values(
                user_id=current_user.id,
                endpoint=body.endpoint,
                p256dh=body.keys.p256dh,
                auth=body.keys.auth,
            )
            .on_conflict_do_update(
                index_elements=["endpoint"],
                set_={
                    "user_id": current_user.id,
                    "p256dh": body.keys.p256dh,
                    "auth": body.keys.auth,
                },
            )
        )
        await session.execute(stmt)
        await session.commit()
    return {"status": "subscribed"}


@router.delete("/subscribe", status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe(
    body: PushUnsubscribeRequest,
    current_user: User = Depends(get_current_user),
) -> Response:
    async with get_async_session() as session:
        await session.execute(
            delete(PushSubscription).where(
                PushSubscription.endpoint == body.endpoint,
                PushSubscription.user_id == current_user.id,
            )
        )
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
