"""Site-expansion eval: first-exposure runs on newly added retailer configs.

Each case forces ONE new marketplace with a category-appropriate query and
ZERO per-site tuning — the first attempt is a held-out generalization data
point, recorded before anyone reads a trace. Losers get disabled, not tuned.

Invocation:
    python -m tests.evals.run_site_expansion --dry-run
    python -m tests.evals.run_site_expansion            # [SPEND]
    python -m tests.evals.run_site_expansion --cases openbox_ca refurbio
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent


def _load_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(str(REPO_ROOT / ".env"))
    if "AGENT_BROWSER_BACKEND" not in os.environ:
        os.environ["AGENT_BROWSER_BACKEND"] = "local"


def _specs():
    from dealbot.schemas import WatchlistContext

    laptop = WatchlistContext(
        product_query="MacBook Air M2",
        max_budget=1100.0,
        condition=["used", "refurb"],
        brands=["Apple"],
        keywords=["macbook air", "m2"],
    )
    pc_laptop = WatchlistContext(
        product_query="ThinkPad X1 Carbon",
        max_budget=900.0,
        condition=["used", "refurb"],
        brands=["Lenovo"],
        keywords=["thinkpad", "x1 carbon"],
    )
    dell_laptop = WatchlistContext(
        product_query="Dell XPS 13",
        max_budget=900.0,
        condition=["used", "refurb"],
        brands=["Dell"],
        keywords=["xps 13"],
    )
    headphones = WatchlistContext(
        product_query="Sony WH-1000XM5 headphones",
        max_budget=350.0,
        condition=["used", "refurb"],
        brands=["Sony"],
        keywords=["wh-1000xm5"],
    )
    return laptop, pc_laptop, dell_laptop, headphones


def _define_cases():
    """(case_id, spec, marketplaces, queries) — one new site each, 1 query
    to keep first-exposure cost ~$0.2-0.5/site."""
    laptop, pc_laptop, dell_laptop, headphones = _specs()
    return [
        ("expand_bestbuy_outlet",   laptop,     ["bestbuy_outlet"],   ["MacBook Air M2"]),
        ("expand_apple_refurbished", laptop,    ["apple_refurbished"], ["MacBook Air M2"]),
        ("expand_canada_computers", pc_laptop,  ["canada_computers"], ["ThinkPad X1 Carbon"]),
        ("expand_visions_openbox",  headphones, ["visions_openbox"],  ["Sony WH-1000XM5"]),
        ("expand_newegg_ca",        pc_laptop,  ["newegg_ca"],        ["ThinkPad X1 Carbon"]),
        ("expand_openbox_ca",       laptop,     ["openbox_ca"],       ["MacBook Air M2"]),
        ("expand_dell_refurbished", dell_laptop, ["dell_refurbished"], ["Dell XPS 13"]),
        ("expand_refurbio",         pc_laptop,  ["refurbio"],         ["ThinkPad X1 Carbon"]),
    ]


def dry_run() -> int:
    for case_id, spec, marketplaces, queries in _define_cases():
        print(f"  {case_id}: {marketplaces[0]} ← {queries[0]!r} (budget ${spec.max_budget})")
    return 0


async def real_run(only_cases: list[str] | None = None) -> int:
    from tests.evals.harness import append_result, run_case

    cases = _define_cases()
    if only_cases:
        cases = [c for c in cases if c[0] in only_cases]

    print("Site Expansion — First Exposure Runs (zero per-site tuning)")
    print("=" * 64)
    worked = 0
    for case_id, spec, marketplaces, queries in cases:
        print(f"\nRunning {case_id}...")
        try:
            t0 = time.monotonic()
            result = await run_case(
                case_id=case_id,
                spec=spec,
                marketplaces=marketplaces,
                queries=queries,
            )
            append_result(result)
            ok = result.offers_unique > 0
            worked += int(ok)
            print(
                f"  {'WORKS' if ok else 'no offers'}: listings={result.offers_total} "
                f"unique={result.offers_unique} wall={result.wall_clock_s:.0f}s "
                f"cost=${result.est_cost_usd:.2f}"
            )
        except Exception as exc:
            print(f"  ERROR: {type(exc).__name__}: {exc}")

    print("\n" + "=" * 64)
    print(f"HEADLINE: {worked}/{len(cases)} new sites productive on first exposure, zero tuning")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cases", nargs="+", default=None)
    args = parser.parse_args()
    if args.dry_run:
        return dry_run()
    _load_env()
    return asyncio.run(real_run(only_cases=args.cases))


if __name__ == "__main__":
    sys.exit(main())
