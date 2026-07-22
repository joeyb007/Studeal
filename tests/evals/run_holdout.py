"""Generalization holdout eval: tuned sites vs held-out sites.

Two tuned cases (kijiji, fb_marketplace) and three held-out cases (ebay,
craigslist Aeron; ebay Sony WH-1000XM5) run with the same fixed queries and
spec. Prints per-case metrics and the headline precision comparison.

IMPORTANT: No tuning in response to held-out results — holdout is single-shot.
eBay and Craigslist receive zero prompt/code tuning at any point.

Invocation:
    python -m tests.evals.run_holdout --dry-run
    python -m tests.evals.run_holdout          # [SPEND] requires user go-ahead
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent


def _load_env() -> None:
    from dotenv import load_dotenv

    env_path = REPO_ROOT / ".env"
    load_dotenv(str(env_path))

    if "AGENT_BROWSER_BACKEND" not in os.environ:
        os.environ["AGENT_BROWSER_BACKEND"] = "local"


def _build_aeron_spec():
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


def _build_headphones_spec():
    from dealbot.schemas import WatchlistContext

    spec = WatchlistContext(
        product_query="Sony WH-1000XM5 headphones",
        max_budget=250.0,
        condition=["used", "like new"],
        brands=["Sony"],
        keywords=["wh-1000xm5", "headphones"],
    )
    fixed_queries = [
        "Sony WH-1000XM5 used",
        "WH-1000XM5 headphones",
        "Sony WH1000XM5 noise cancelling",
    ]
    return spec, fixed_queries


def _define_cases():
    """Return list of (case_id, arm, spec, marketplaces, fixed_queries) tuples."""
    aeron_spec, aeron_queries = _build_aeron_spec()
    hp_spec, hp_queries = _build_headphones_spec()

    return [
        # Tuned arm
        ("tuned_kijiji",          "tuned",   aeron_spec, ["kijiji"],        aeron_queries),
        ("tuned_fb",              "tuned",   aeron_spec, ["fb_marketplace"], aeron_queries),
        # Held-out arm
        ("holdout_ebay_aeron",    "holdout", aeron_spec, ["ebay"],           aeron_queries),
        ("holdout_craigslist_aeron", "holdout", aeron_spec, ["craigslist"],  aeron_queries),
        ("holdout_ebay_headphones", "holdout", hp_spec,   ["ebay"],          hp_queries),
    ]


def dry_run() -> int:
    """Print the 5-case plan without any network I/O."""
    cases = _define_cases()

    print("Generalization Holdout Eval — 5-Case Plan")
    print("=" * 60)
    print()

    for case_id, arm, spec, marketplaces, queries in cases:
        label = "[TUNED]" if arm == "tuned" else "[HOLDOUT]"
        print(f"  {label} {case_id}")
        print(f"    spec:        {spec.product_query}")
        print(f"    brands:      {spec.brands}")
        print(f"    max_budget:  ${spec.max_budget}")
        print(f"    marketplaces:{marketplaces}")
        print(f"    queries:     {queries}")
        print()

    print("NOTE: no tuning in response to held-out results — holdout is single-shot.")
    print()
    return 0


async def real_run() -> int:
    """Run all 5 cases sequentially, print per-case metrics and headline."""
    from tests.evals.harness import append_result, run_case

    cases = _define_cases()

    print("Generalization Holdout Eval — Real Run")
    print("=" * 60)
    print()

    tuned_precisions: list[float] = []
    holdout_precisions: list[float] = []

    for case_id, arm, spec, marketplaces, queries in cases:
        label = "[TUNED]" if arm == "tuned" else "[HOLDOUT]"
        print(f"{label} Running {case_id}...")

        try:
            t0 = time.monotonic()
            result = await run_case(
                case_id=case_id,
                spec=spec,
                marketplaces=marketplaces,
                entry_mode="template",
                queries=queries,
                trace_root="traces/evals",
            )
            elapsed = time.monotonic() - t0

            prec_str = f"{result.precision:.2%}" if result.precision is not None else "N/A (0 offers)"
            print(
                f"  listings={result.offers_total} "
                f"unique={result.offers_unique} "
                f"precision={prec_str} "
                f"wall_clock={elapsed:.1f}s "
                f"cost=${result.est_cost_usd:.3f}"
            )

            if arm == "tuned":
                if result.precision is not None:
                    tuned_precisions.append(result.precision)
            else:
                if result.precision is not None:
                    holdout_precisions.append(result.precision)

            append_result(result)

        except Exception as e:
            print(f"  ERROR | {e}")
            return 1

        print()

    # Headline comparison
    print("=" * 60)
    print("HEADLINE: Tuned vs Held-Out Precision")
    print()

    if tuned_precisions:
        mean_tuned = sum(tuned_precisions) / len(tuned_precisions)
        print(f"  Mean precision (tuned sites):   {mean_tuned:.2%}  (n={len(tuned_precisions)})")
    else:
        print("  Mean precision (tuned sites):   N/A (no offers extracted)")

    if holdout_precisions:
        mean_holdout = sum(holdout_precisions) / len(holdout_precisions)
        print(f"  Mean precision (held-out sites):{mean_holdout:.2%}  (n={len(holdout_precisions)})")
        if tuned_precisions:
            drop = (mean_tuned - mean_holdout) / mean_tuned * 100 if mean_tuned > 0 else float("nan")
            print(f"  Precision drop:                 {drop:.1f}%")
    else:
        print("  HOLDOUT: no offers extracted — no precision claim")

    print()
    print("NOTE: no tuning in response to held-out results — holdout is single-shot.")
    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generalization holdout eval: tuned vs held-out sites"
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
