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
