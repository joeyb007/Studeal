"""Serve user-uploaded inspection media (authenticated; keys are opaque)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response

from dealbot.api.auth import get_current_user
from dealbot.db.models import User
from dealbot.media import content_type_for, load_image

router = APIRouter(prefix="/media", tags=["media"])


@router.get("/{key:path}")
async def read_media(
    key: str,
    current_user: User = Depends(get_current_user),
) -> Response:
    data = await load_image(key)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such media.")
    return Response(
        content=data,
        media_type=content_type_for(key),
        headers={"Cache-Control": "private, max-age=86400"},
    )
