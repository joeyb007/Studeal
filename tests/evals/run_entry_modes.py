"""Entry-mode comparison: URL-template vs home-page navigation.

Grid: 2 marketplaces × 2 entry modes = 4 cells.
  Marketplaces: kijiji (tuned), ebay (held-out)
  Entry modes:  template, home

This is the measured answer to "why URL templates" for the blog.

Invocation:
    python -m tests.evals.run_entry_modes --dry-run
    python -m tests.evals.run_entry_modes
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent

_MARKETPLACES = ["kijiji", "ebay"]
_ENTRY_MODES = ["template", "home"]
_FIXED_QUERY = "Herman Miller Aeron office chair used"


def _load_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(str(REPO_ROOT / ".env"))
    if "AGENT_BROWSER_BACKEND" not in os.environ:
        os.environ["AGENT_BROWSER_BACKEND"] = "local"


def _build_spec():
    from dealbot.schemas import WatchlistContext

    return WatchlistContext(
        product_query="Herman Miller Aeron chair Toronto",
        max_budget=700.0,
        condition=["used", "like new"],
        brands=["Herman Miller"],
        keywords=["aeron", "office chair"],
    )


def dry_run() -> int:
    spec = _build_spec()
    print("Entry-Mode Comparison — 4-Cell Plan")
    print("=" * 60)
    print(f"Spec:        {spec.product_query}")
    print(f"Max budget:  ${spec.max_budget}")
    print(f"Fixed query: {_FIXED_QUERY!r}")
    print()
    print(f"{'Cell':<30}  {'Marketplace':<12}  {'Mode'}")
    print("-" * 60)
    for marketplace in _MARKETPLACES:
        for mode in _ENTRY_MODES:
            cell_id = f"entry_{marketplace}_{mode}"
            label = "(tuned)" if marketplace == "kijiji" else "(held-out)"
            print(f"  {cell_id:<28}  {marketplace:<12}  {mode}  {label}")
    print()
    print("4 runs total — each is a single-marketplace, single-query call.")
    return 0


async def real_run() -> int:
    from tests.evals.harness import append_result, run_case

    spec = _build_spec()
    results = []

    print("Entry-Mode Comparison — Real Run")
    print("=" * 60)
    print()

    for marketplace in _MARKETPLACES:
        for mode in _ENTRY_MODES:
            case_id = f"entry_{marketplace}_{mode}"
            label = "(tuned)" if marketplace == "kijiji" else "(held-out)"
            print(f"Running {case_id} {label}...")

            result = await run_case(
                case_id=case_id,
                spec=spec,
                marketplaces=[marketplace],
                entry_mode=mode,
                queries=[_FIXED_QUERY],
                trace_root="traces/evals",
            )

            stop_summary = ", ".join(
                f"{k.split('/')[0]}={v}" for k, v in result.stop_reasons.items()
            )
            nav_note = "nav-heavy" if mode == "home" else "direct"
            print(
                f"  {marketplace:<12}  {mode:<8}  "
                f"listings={result.offers_total}  "
                f"unique={result.offers_unique}  "
                f"wall_clock={result.wall_clock_s:.1f}s  "
                f"[{nav_note}]  "
                f"mkt_w_listings={result.marketplaces_with_listings}  "
                f"stops=({stop_summary})  "
                f"est_cost=${result.est_cost_usd:.4f}"
            )

            append_result(result)
            results.append(result)
            print()

    # Summary table
    print("=" * 60)
    print("Summary: marketplace × mode → listings | wall_clock | cost")
    print()
    print(f"{'marketplace':<12}  {'mode':<8}  {'listings':>8}  {'wall_clock(s)':>13}  {'est_cost($)':>11}")
    print("-" * 60)
    for r in results:
        mkt = r.marketplaces[0]
        print(
            f"{mkt:<12}  {r.entry_mode:<8}  {r.offers_total:>8}  "
            f"{r.wall_clock_s:>13.1f}  {r.est_cost_usd:>11.4f}"
        )

    # One-line takeaway per site
    print()
    for marketplace in _MARKETPLACES:
        site_results = {r.entry_mode: r for r in results if r.marketplaces[0] == marketplace}
        tmpl = site_results.get("template")
        home = site_results.get("home")
        if tmpl and home:
            delta_listings = tmpl.offers_total - home.offers_total
            delta_time = home.wall_clock_s - tmpl.wall_clock_s
            print(
                f"Takeaway [{marketplace}]: template yields "
                f"{delta_listings:+d} listings vs home "
                f"({delta_time:+.1f}s wall-clock for home navigation)."
            )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Entry-mode comparison: URL-template vs home-page navigation"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print 4-cell plan and exit without network I/O",
    )
    args = parser.parse_args()

    if args.dry_run:
        return dry_run()
    else:
        _load_env()
        return asyncio.run(real_run())


if __name__ == "__main__":
    sys.exit(main())
