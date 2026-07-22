"""Reliability campaign: run 5 consecutive Aeron/GTA cases with abort-fast.

This script drives the standard reliability case (5 runs) with sequential execution,
abort-fast on first-two failures, and result recording. Entry point only — no pytest
collection.

Invocation:
    python -m tests.evals.run_reliability --dry-run
    python -m tests.evals.run_reliability
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Must run from repo root
REPO_ROOT = Path(__file__).parent.parent.parent


def _load_env() -> None:
    """Load .env explicitly, set AGENT_BROWSER_BACKEND to 'local' if unset."""
    from dotenv import load_dotenv

    env_path = REPO_ROOT / ".env"
    load_dotenv(str(env_path))

    if "AGENT_BROWSER_BACKEND" not in os.environ:
        os.environ["AGENT_BROWSER_BACKEND"] = "local"


def _build_standard_case():
    """Build the standard Aeron/GTA spec and fixed query list."""
    from dealbot.schemas import WatchlistContext

    spec = WatchlistContext(
        product_query="Herman Miller Aeron chair Toronto",
        max_budget=700.0,
        condition=["used", "like new"],
        brands=["Herman Miller"],
        keywords=["aeron", "office chair"],
    )

    marketplaces = ["kijiji", "fb_marketplace", "craigslist"]
    entry_mode = "template"
    fixed_queries = [
        "Herman Miller Aeron office chair used",
        "used Aeron chair Herman Miller",
        "like new Herman Miller Aeron chair",
    ]

    return spec, marketplaces, entry_mode, fixed_queries


def dry_run() -> int:
    """Print the 5-run plan without network I/O, then exit 0."""
    spec, marketplaces, entry_mode, fixed_queries = _build_standard_case()

    print("Reliability Campaign — 5-Run Plan")
    print("=" * 60)
    print(f"Spec: {spec.product_query}")
    print(f"  Max budget: ${spec.max_budget}")
    print(f"  Condition: {spec.condition}")
    print(f"  Brands: {spec.brands}")
    print(f"  Keywords: {spec.keywords}")
    print(f"Entry mode: {entry_mode}")
    print(f"Marketplaces: {marketplaces}")
    print(f"Fixed queries: {fixed_queries}")
    print()
    print("Runs:")
    for i in range(1, 6):
        case_id = f"reliability_run_{i}"
        print(f"  {case_id}")
    print()
    return 0


async def real_run() -> int:
    """Run 5 sequential cases with abort-fast on first-two failures."""
    from tests.evals.harness import append_result, run_case

    spec, marketplaces, entry_mode, fixed_queries = _build_standard_case()

    results = []
    passed_count = 0

    print("Reliability Campaign — Real Run")
    print("=" * 60)
    print()

    for i in range(1, 6):
        case_id = f"reliability_run_{i}"
        print(f"Run {i}/5: {case_id}...")

        try:
            result = await run_case(
                case_id=case_id,
                spec=spec,
                marketplaces=marketplaces,
                entry_mode=entry_mode,
                queries=fixed_queries,
                trace_root="traces/evals",
            )
            results.append(result)

            # Print one-line summary
            status = "PASS" if result.passed_bar else "FAIL"
            print(
                f"  {status} | offers={result.offers_total} "
                f"unique={result.offers_unique} | "
                f"mkt_w_listings={result.marketplaces_with_listings} | "
                f"wall_clock={result.wall_clock_s:.1f}s | "
                f"errors={result.error_stops} | "
                f"cost=${result.est_cost_usd:.3f}"
            )

            if result.passed_bar:
                passed_count += 1

            # Abort-fast: if runs 1 and 2 both failed, stop
            if i == 2 and passed_count == 0:
                print()
                print("ABORT-FAST: first two runs failed the bar")
                print(f"RELIABILITY: {passed_count}/5")
                return 1

            # Append result after each run
            append_result(result)

        except Exception as e:
            print(f"  ERROR | {e}")
            return 1

        print()

    # Final verdict
    print("=" * 60)
    print(f"RELIABILITY: {passed_count}/5")
    total_cost = sum(r.est_cost_usd for r in results)
    print(f"Total estimated cost: ${total_cost:.3f}")
    print()
    return 0 if passed_count >= 3 else 1


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Reliability campaign: 5 Aeron/GTA runs with abort-fast"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan and exit without network I/O",
    )

    args = parser.parse_args()

    if args.dry_run:
        return dry_run()
    else:
        _load_env()
        return asyncio.run(real_run())


if __name__ == "__main__":
    sys.exit(main())
