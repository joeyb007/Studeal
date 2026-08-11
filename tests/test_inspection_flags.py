"""Flags backbone (spec 2026-08-10): objective flags derived from Tier A
reports, and the tolerant quality-bar context field."""

from dealbot.agents.inspector import derive_flags
from dealbot.schemas import WatchlistContext


def _report(**overrides) -> dict:
    base = {
        "headline": "Solid pair, honest wear.",
        "condition_grade": "fair",
        "photos_real": True,
        "identification": "AirPods Max, 2020",
        "condition": "Light scuffs on the headband.",
        "red_flags": [],
        "cant_tell": "Battery health.",
        "seller_questions": ["Battery percentage after an hour?"],
        "legitimacy": {"level": "fine", "reason": "Normal listing."},
        "market_position": "Near the median.",
        "summary": "Fine deal.",
    }
    base.update(overrides)
    return base


def test_derive_flags_none_without_report():
    assert derive_flags(None) is None


def test_derive_flags_carries_objective_fields():
    flags = derive_flags(_report())
    assert flags == {
        "photos_real": True,
        "condition_grade": "fair",
        "legit_level": "fine",
        "legit_reason": "Normal listing.",
    }


def test_derive_flags_non_bool_photos_real_degrades_to_none():
    flags = derive_flags(_report(photos_real="yes"))
    assert flags["photos_real"] is None


def test_derive_flags_missing_legitimacy_degrades():
    report = _report()
    del report["legitimacy"]
    flags = derive_flags(report)
    assert flags["legit_level"] is None
    assert flags["legit_reason"] is None


def test_quality_bar_accepts_known_labels():
    ctx = WatchlistContext(product_query="airpods max", quality_bar="wear_ok")
    assert ctx.quality_bar == "wear_ok"


def test_quality_bar_unknown_label_degrades_to_none():
    ctx = WatchlistContext(product_query="airpods max", quality_bar="mint")
    assert ctx.quality_bar is None


def test_context_roundtrips_appearance_notes():
    ctx = WatchlistContext(product_query="x", appearance_notes="no dents on the cups")
    again = WatchlistContext.model_validate_json(ctx.model_dump_json())
    assert again.appearance_notes == "no dents on the cups"


# ---- concurrency: one visit per listing, second caller reads the cache ----

import asyncio

import pytest


@pytest.mark.asyncio
async def test_concurrent_inspections_deduplicate(monkeypatch):
    from dealbot.agents import inspector

    runs = {"count": 0}
    cache: dict[int, dict] = {}

    async def fake_cached(listing_id):
        return cache.get(listing_id)

    async def fake_run(listing_id, force):
        runs["count"] += 1
        await asyncio.sleep(0.05)          # a slow browser visit
        result = {"status": "ok", "run": runs["count"]}
        cache[listing_id] = result
        return result

    monkeypatch.setattr(inspector, "get_cached_inspection", fake_cached)
    monkeypatch.setattr(inspector, "_run_inspection", fake_run)
    inspector._inspection_locks.clear()

    first, second = await asyncio.gather(
        inspector.get_or_create_inspection(42),
        inspector.get_or_create_inspection(42),
    )
    assert runs["count"] == 1               # one visit, not two
    assert first == second == {"status": "ok", "run": 1}


@pytest.mark.asyncio
async def test_different_listings_do_not_serialize(monkeypatch):
    from dealbot.agents import inspector

    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_cached(listing_id):
        return None

    async def fake_run(listing_id, force):
        if listing_id == 1:
            started.set()
            await release.wait()
        return {"status": "ok", "id": listing_id}

    monkeypatch.setattr(inspector, "get_cached_inspection", fake_cached)
    monkeypatch.setattr(inspector, "_run_inspection", fake_run)
    inspector._inspection_locks.clear()

    slow = asyncio.create_task(inspector.get_or_create_inspection(1))
    await started.wait()
    # Listing 2 completes while listing 1 is still mid-visit.
    fast = await asyncio.wait_for(inspector.get_or_create_inspection(2), timeout=1.0)
    assert fast["id"] == 2
    release.set()
    assert (await slow)["id"] == 1


def test_image_mime_sniffing():
    from dealbot.agents.inspector import _sniff_image_mime
    assert _sniff_image_mime(b"\xff\xd8\xff\xe0" + b"x" * 8) == "image/jpeg"
    assert _sniff_image_mime(b"\x89PNG\r\n\x1a\n" + b"x" * 8) == "image/png"
    assert _sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 8) == "image/webp"
    assert _sniff_image_mime(b"GIF89a" + b"x" * 8) == "image/gif"
    assert _sniff_image_mime(b"unknown-bytes") == "image/jpeg"


def test_mm_embed_payload_shape():
    from dealbot.llm.bedrock_client import build_mm_embed_payload
    both = build_mm_embed_payload("golf driver", "aGVsbG8=")
    assert both == {"embeddingConfig": {"outputEmbeddingLength": 1024},
                    "inputText": "golf driver", "inputImage": "aGVsbG8="}
    text_only = build_mm_embed_payload("golf driver", None)
    assert "inputImage" not in text_only
    image_only = build_mm_embed_payload("  ", "aGVsbG8=")
    assert "inputText" not in image_only
