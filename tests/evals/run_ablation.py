"""Ablation study: Studeal pipeline vs. NaiveReActRunner across 2 models.

Grid: 2 systems × 2 models = 4 cells, all on the standard Aeron/GTA case.

  studeal/gpt-4o         — harness.run_case with AGENT_NAV_MODEL=gpt-4o
  studeal/gpt-4o-mini    — harness.run_case with AGENT_NAV_MODEL=gpt-4o-mini
  naive/gpt-4o           — NaiveReActRunner with OpenAIClient(model=gpt-4o)
  naive/gpt-4o-mini      — NaiveReActRunner with OpenAIClient(model=gpt-4o-mini)

NOTE: Third model (Groq) cut per plan budget rule.

Invocation:
    python -m tests.evals.run_ablation --dry-run
    python -m tests.evals.run_ablation        # [SPEND] — requires explicit go
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent

_MODELS = ["gpt-4o", "gpt-4o-mini"]
_MARKETPLACES = ["kijiji", "fb_marketplace", "craigslist"]

# Per-token rates for naive cost estimate (mirrors harness.py constants)
_RATES: dict[str, dict[str, float]] = {
    "gpt-4o": {
        "prompt": 2.50 / 1_000_000,
        "completion": 10.00 / 1_000_000,
    },
    "gpt-4o-mini": {
        "prompt": 0.15 / 1_000_000,
        "completion": 0.60 / 1_000_000,
    },
}


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def _load_env() -> None:
    from dotenv import load_dotenv
    load_dotenv(str(REPO_ROOT / ".env"))
    if "AGENT_BROWSER_BACKEND" not in os.environ:
        os.environ["AGENT_BROWSER_BACKEND"] = "local"


def _build_standard_case():
    from dealbot.schemas import WatchlistContext
    spec = WatchlistContext(
        product_query="Herman Miller Aeron chair Toronto",
        max_budget=700.0,
        condition=["used", "like new"],
        brands=["Herman Miller"],
        keywords=["aeron", "office chair"],
    )
    fixed_queries = [
        "Herman Miller Aeron office chair used",
        "used Aeron chair Herman Miller",
        "like new Herman Miller Aeron chair",
    ]
    return spec, fixed_queries


# ---------------------------------------------------------------------------
# Dry-run: print 4-cell grid plan, no network
# ---------------------------------------------------------------------------

def dry_run() -> int:
    print("Ablation Campaign — 4-Cell Grid Plan")
    print("=" * 60)
    print(f"Marketplaces: {_MARKETPLACES}")
    print(f"Models: {_MODELS}")
    print()
    print("NOTE: Third model (Groq) cut per plan budget rule.")
    print()
    print(f"{'Cell':<28} {'System':<10} {'Model':<14} {'Marketplaces'}")
    print("-" * 70)
    for model in _MODELS:
        print(f"  studeal/{model:<20} studeal    {model:<14} {_MARKETPLACES}")
    for model in _MODELS:
        print(f"  naive/{model:<22} naive      {model:<14} {_MARKETPLACES}")
    print()
    print("Output: docs/evals/results.jsonl + docs/evals/results.md")
    print("Traces: traces/evals/ablation_*/")
    return 0


# ---------------------------------------------------------------------------
# Studeal cell runner
# ---------------------------------------------------------------------------

async def _run_studeal_cell(model: str, spec, fixed_queries: list[str]) -> dict:
    from tests.evals.harness import append_result, run_case

    os.environ["AGENT_NAV_MODEL"] = model
    case_id = f"ablation_studeal_{model.replace('-', '_')}"

    print(f"  Running studeal/{model}...")
    t0 = time.monotonic()
    result = await run_case(
        case_id=case_id,
        spec=spec,
        marketplaces=_MARKETPLACES,
        entry_mode="template",
        queries=fixed_queries,
        trace_root="traces/evals",
    )
    wall = time.monotonic() - t0

    append_result(result)

    return {
        "cell": f"studeal/{model}",
        "offers": result.offers_total,
        "unique": result.offers_unique,
        "precision": result.precision,
        "wall_clock_s": wall,
        "est_cost_usd": result.est_cost_usd,
        "passed_bar": result.passed_bar,
        "result": result,
    }


# ---------------------------------------------------------------------------
# Naive cell runner
# ---------------------------------------------------------------------------

async def _run_naive_cell(model: str, spec, fixed_queries: list[str]) -> dict:
    from dealbot.llm.openai_client import OpenAIClient
    from dealbot.persistence.canonicalize import canonicalize_url
    from dealbot.scrapers.browser_session import LocalPlaywrightSession
    from tests.evals.harness import (
        CaseResult,
        _resolve_targets,
        append_result,
        offer_matches_spec,
    )
    from tests.evals.naive_react import NaiveReActRunner

    api_key = os.environ.get("OPENAI_API_KEY", "")
    llm = OpenAIClient(model=model, api_key=api_key)
    runner = NaiveReActRunner(llm)

    # Resolve targets for ALL fixed queries so naive gets the same 3×3 coverage
    # as Studeal's harness.run_case (which uses all fixed_queries).
    all_targets = []
    for q in fixed_queries:
        all_targets.extend(_resolve_targets(_MARKETPLACES, q, "template"))
    targets = all_targets  # 3 queries × 3 marketplaces = 9, same coverage as Studeal

    fb_state = os.environ.get("FB_STATE_PATH")
    storage_state = fb_state if fb_state and os.path.isfile(fb_state) else None

    case_id = f"ablation_naive_{model.replace('-', '_')}"
    print(f"  Running naive/{model}...")

    t0 = time.monotonic()
    async with LocalPlaywrightSession(storage_state=storage_state) as session:
        naive_result = await runner.run(spec=spec, targets=targets, session=session)
    wall = time.monotonic() - t0

    # Unique by canonicalized URL
    seen: set[str] = set()
    unique_offers = []
    for offer in naive_result.offers:
        canon = canonicalize_url(offer.url, offer.marketplace)
        if canon not in seen:
            seen.add(canon)
            unique_offers.append(offer)

    # Precision heuristic (same as harness)
    total = len(naive_result.offers)
    if total == 0:
        precision: float | None = None
    else:
        matched = sum(1 for o in naive_result.offers if offer_matches_spec(o, spec))
        precision = matched / total

    # Cost estimate
    prompt_tokens = getattr(llm, "total_prompt_tokens", 0)
    completion_tokens = getattr(llm, "total_completion_tokens", 0)
    rate = _RATES.get(model, _RATES["gpt-4o-mini"])
    est_cost = (
        prompt_tokens * rate["prompt"]
        + completion_tokens * rate["completion"]
    )

    # Build CaseResult for recording (naive cells use entry_mode="naive_react")
    offers_by_marketplace: dict[str, int] = {}
    for offer in naive_result.offers:
        offers_by_marketplace[offer.marketplace] = (
            offers_by_marketplace.get(offer.marketplace, 0) + 1
        )
    marketplaces_with_listings = sum(1 for c in offers_by_marketplace.values() if c > 0)

    case_result = CaseResult(
        case_id=case_id,
        spec_query=spec.product_query,
        marketplaces=_MARKETPLACES,
        entry_mode="naive_react",
        offers_total=total,
        offers_unique=len(unique_offers),
        offers_by_marketplace=offers_by_marketplace,
        marketplaces_with_listings=marketplaces_with_listings,
        wall_clock_s=wall,
        stop_reasons={"all": naive_result.stop_reason},
        error_stops=1 if naive_result.stop_reason == "error" else 0,
        nav_prompt_tokens=prompt_tokens,
        nav_completion_tokens=completion_tokens,
        extract_prompt_tokens=0,
        extract_completion_tokens=0,
        est_cost_usd=est_cost,
        passed_bar=(
            total >= 10
            and marketplaces_with_listings >= 2
            and wall < 240.0
            and naive_result.stop_reason != "error"
        ),
        precision=precision,
    )
    append_result(case_result)

    return {
        "cell": f"naive/{model}",
        "offers": total,
        "unique": len(unique_offers),
        "precision": precision,
        "wall_clock_s": wall,
        "est_cost_usd": est_cost,
        "passed_bar": case_result.passed_bar,
        "result": case_result,
    }


# ---------------------------------------------------------------------------
# Real run: sequential 4-cell grid
# ---------------------------------------------------------------------------

async def real_run() -> int:
    spec, fixed_queries = _build_standard_case()

    rows: list[dict] = []

    print("Ablation Campaign — Real Run")
    print("=" * 60)
    print("NOTE: Third model (Groq) cut per plan budget rule.")
    print()

    # Studeal cells
    for model in _MODELS:
        try:
            row = await _run_studeal_cell(model, spec, fixed_queries)
            rows.append(row)
            _print_row(row)
        except Exception as exc:
            print(f"  ERROR studeal/{model}: {exc}")
            rows.append({
                "cell": f"studeal/{model}",
                "offers": 0, "unique": 0, "precision": None,
                "wall_clock_s": 0.0, "est_cost_usd": 0.0, "passed_bar": False,
            })

    # Naive cells
    for model in _MODELS:
        try:
            row = await _run_naive_cell(model, spec, fixed_queries)
            rows.append(row)
            _print_row(row)
        except Exception as exc:
            print(f"  ERROR naive/{model}: {exc}")
            rows.append({
                "cell": f"naive/{model}",
                "offers": 0, "unique": 0, "precision": None,
                "wall_clock_s": 0.0, "est_cost_usd": 0.0, "passed_bar": False,
            })

    # Pareto table
    print()
    print("=" * 60)
    print("Pareto Table")
    print("=" * 60)
    _print_table(rows)
    print()
    total_cost = sum(r.get("est_cost_usd", 0.0) for r in rows)
    print(f"Total estimated cost: ${total_cost:.3f}")
    return 0


def _print_row(row: dict) -> None:
    prec = f"{row['precision']:.2f}" if row.get("precision") is not None else "N/A"
    bar = "PASS" if row.get("passed_bar") else "FAIL"
    print(
        f"  {row['cell']:<28} offers={row['offers']} unique={row['unique']} "
        f"precision={prec} wall={row['wall_clock_s']:.1f}s "
        f"cost=${row['est_cost_usd']:.3f} bar={bar}"
    )


def _print_table(rows: list[dict]) -> None:
    header = f"{'Cell':<28} {'Offers':>7} {'Unique':>7} {'Precision':>10} {'Wall(s)':>8} {'Cost($)':>8} {'Bar':>5}"
    print(header)
    print("-" * len(header))
    for row in rows:
        prec = f"{row['precision']:.2f}" if row.get("precision") is not None else "N/A"
        bar = "PASS" if row.get("passed_bar") else "FAIL"
        print(
            f"{row['cell']:<28} {row['offers']:>7} {row['unique']:>7} "
            f"{prec:>10} {row['wall_clock_s']:>8.1f} "
            f"{row['est_cost_usd']:>8.3f} {bar:>5}"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ablation: Studeal vs NaiveReActRunner across 2 models"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print 4-cell grid plan without network I/O",
    )
    args = parser.parse_args()

    if args.dry_run:
        return dry_run()

    _load_env()
    return asyncio.run(real_run())


if __name__ == "__main__":
    sys.exit(main())
