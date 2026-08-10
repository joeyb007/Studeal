"""User-uploaded media for inspection chats (copilot spec 2026-08-10, phase B).

Local disk in dev, S3 in prod, one interface. Keys are opaque
uuid-based paths ("inspections/ab12….jpg"); nothing user-controlled ever
touches the filesystem path.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


def _backend() -> str:
    return os.environ.get("MEDIA_BACKEND", "local")


def _local_dir() -> Path:
    return Path(os.environ.get("MEDIA_DIR", "./media"))


def new_key(content_type: str) -> str:
    return f"inspections/{uuid.uuid4().hex}{ALLOWED_IMAGE_TYPES[content_type]}"


def content_type_for(key: str) -> str:
    for content_type, ext in ALLOWED_IMAGE_TYPES.items():
        if key.endswith(ext):
            return content_type
    return "application/octet-stream"


async def save_image(data: bytes, content_type: str) -> str:
    """Persist one validated image; returns its media key. Raises ValueError
    on type/size violations (callers translate to 4xx)."""
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise ValueError("Unsupported image type.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image too large (5MB max).")
    if not data:
        raise ValueError("Empty upload.")
    key = new_key(content_type)

    if _backend() == "s3":
        import aiobotocore.session

        bucket = os.environ["MEDIA_S3_BUCKET"]
        session = aiobotocore.session.get_session()
        async with session.create_client(
            "s3", region_name=os.environ.get("AWS_REGION", "us-east-1")
        ) as client:
            await client.put_object(
                Bucket=bucket, Key=key, Body=data, ContentType=content_type,
            )
        return key

    path = _local_dir() / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return key


async def load_image(key: str) -> bytes | None:
    """Bytes for a stored key; None when missing. Keys are validated against
    the uuid shape so a crafted key can never traverse paths."""
    name = key.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    if not (key.startswith("inspections/") and len(stem) == 32 and stem.isalnum()):
        return None

    if _backend() == "s3":
        import aiobotocore.session

        bucket = os.environ["MEDIA_S3_BUCKET"]
        session = aiobotocore.session.get_session()
        try:
            async with session.create_client(
                "s3", region_name=os.environ.get("AWS_REGION", "us-east-1")
            ) as client:
                response = await client.get_object(Bucket=bucket, Key=key)
                return await response["Body"].read()
        except Exception:
            logger.warning("media: s3 read failed for %s", key, exc_info=True)
            return None

    path = _local_dir() / key
    try:
        return path.read_bytes()
    except OSError:
        return None
