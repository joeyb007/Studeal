"""Inspection media store (copilot spec 2026-08-10, phase B): validation,
key shape, and traversal safety on the local backend."""

import pytest

from dealbot.media import load_image, save_image


@pytest.mark.asyncio
async def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    key = await save_image(b"png-bytes-here", "image/png")
    assert key.startswith("inspections/") and key.endswith(".png")
    assert await load_image(key) == b"png-bytes-here"


@pytest.mark.asyncio
async def test_rejects_unsupported_type(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        await save_image(b"x", "application/pdf")
    with pytest.raises(ValueError):
        await save_image(b"x", "image/svg+xml")   # scriptable, never allowed


@pytest.mark.asyncio
async def test_rejects_oversize_and_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        await save_image(b"", "image/jpeg")
    with pytest.raises(ValueError):
        await save_image(b"x" * (5 * 1024 * 1024 + 1), "image/jpeg")


@pytest.mark.asyncio
async def test_crafted_keys_never_read_files(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    secret = tmp_path / "inspections" / "secret.txt"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("nope")
    assert await load_image("inspections/../../etc/passwd") is None
    assert await load_image("inspections/secret.txt") is None      # not uuid-shaped
    assert await load_image("elsewhere/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png") is None
