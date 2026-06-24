"""Multi-trial eval runner for the marketplace-hunter agent.

Runs N trials per spike case, aggregates per-case stats (success rate,
listings p50/p95, latency p50/p95, error breakdown), writes a markdown
report under `docs/evals/v<label>.md`.

Usage:
  ./venv/bin/python scripts/run_evals.py --version v1.0 --trials 5 \\
      --cases kijiji_gta_aeron_fixed fb_marketplace_toronto_aeron_fixed \\
              road_bike_google_freeroam

  # default: 3 trials × the "core" set (Kijiji + FB + road-bike-freeroam)
  ./venv/bin/python scripts/run_evals.py --version v1.0 --trials 3

The eval framework intentionally lives outside pytest so multi-trial
runs don't carry pytest's per-test setup/teardown overhead and can run
unattended for ~hours.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

# Make repo root importable so we can pull from tests/evals/.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(_REPO_ROOT / ".env")

from tests.evals.test_capability_spike import (  # noqa: E402
    CASES,
    SpikeCase,
    SpikeResult,
    run_spike_case,
)


_DEFAULT_CORE_CASES = (
    "kijiji_gta_aeron_fixed",
    "fb_marketplace_toronto_aeron_fixed",
    "road_bike_google_freeroam",
)


def _percentile(values: list[float], p: float) -> float:
    """Simple percentile (linear interpolation between nearest ranks)."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def _aggregate(case_name: str, results: list[SpikeResult]) -> dict:
    """Per-case stats across N trials."""
    n = len(results)
    completed = sum(1 for r in results if r.completed and r.error is None)
    listings = [r.listing_count for r in results]
    latencies = [r.latency_sec for r in results]
    error_kinds = Counter(
        (r.error.split(":", 1)[0] if r.error else "OK") for r in results
    )
    productive = sum(1 for r in results if r.listing_count > 0)
    return {
        "case": case_name,
        "trials": n,
        "completed": completed,
        "completion_rate": (completed / n) if n else 0.0,
        "productive_trials": productive,
        "productive_rate": (productive / n) if n else 0.0,
        "listings_mean": (sum(listings) / n) if n else 0.0,
        "listings_p50": _percentile([float(x) for x in listings], 0.50),
        "listings_p95": _percentile([float(x) for x in listings], 0.95),
        "listings_max": max(listings) if listings else 0,
        "listings_min": min(listings) if listings else 0,
        "latency_p50_s": _percentile(latencies, 0.50),
        "latency_p95_s": _percentile(latencies, 0.95),
        "error_breakdown": dict(error_kinds),
    }


def _write_report(version: str, cases_stats: list[dict], started_at: str, total_wall_s: float) -> Path:
    out_dir = _REPO_ROOT / "docs" / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{version}.md"

    lines: list[str] = [
        f"# Eval — `{version}`",
        "",
        f"Started: {started_at}  ",
        f"Total wall-clock: {total_wall_s / 60:.1f} min  ",
        f"Cases: {len(cases_stats)}  ",
        f"Trials/case: {cases_stats[0]['trials'] if cases_stats else '?'}",
        "",
        "## Summary",
        "",
        "| Case | Trials | Completed | Productive | List p50 | List p95 | Lat p50 | Lat p95 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in cases_stats:
        lines.append(
            f"| {s['case']} | {s['trials']} | "
            f"{s['completed']}/{s['trials']} ({s['completion_rate']*100:.0f}%) | "
            f"{s['productive_trials']}/{s['trials']} ({s['productive_rate']*100:.0f}%) | "
            f"{s['listings_p50']:.0f} | {s['listings_p95']:.0f} | "
            f"{s['latency_p50_s']:.0f}s | {s['latency_p95_s']:.0f}s |"
        )

    lines.extend(["", "## Per-case detail", ""])
    for s in cases_stats:
        lines.append(f"### {s['case']}")
        lines.append("")
        lines.append(
            f"- Trials: **{s['trials']}**  "
            f"completed: **{s['completed']}**  "
            f"productive (≥1 listing): **{s['productive_trials']}**"
        )
        lines.append(
            f"- Listings — mean: **{s['listings_mean']:.1f}**, "
            f"p50: **{s['listings_p50']:.0f}**, p95: **{s['listings_p95']:.0f}**, "
            f"min/max: **{s['listings_min']}** / **{s['listings_max']}**"
        )
        lines.append(
            f"- Latency p50/p95: **{s['latency_p50_s']:.0f}s** / "
            f"**{s['latency_p95_s']:.0f}s**"
        )
        if s["error_breakdown"]:
            lines.append(f"- Errors: `{s['error_breakdown']}`")
        lines.append("")

    out_path.write_text("\n".join(lines))
    return out_path


async def _run_eval(
    version: str,
    case_names: list[str],
    trials: int,
    inter_case_sleep_s: float,
) -> None:
    case_map = {c.name: c for c in CASES}
    missing = [n for n in case_names if n not in case_map]
    if missing:
        raise SystemExit(f"Unknown case names: {missing}. Available: {list(case_map)}")
    cases = [case_map[n] for n in case_names]

    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY must be set (e.g. via .env)")

    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.perf_counter()

    all_results: dict[str, list[SpikeResult]] = {c.name: [] for c in cases}

    for trial_idx in range(1, trials + 1):
        print(f"\n\n========== TRIAL {trial_idx}/{trials} ==========")
        for case in cases:
            result = await run_spike_case(
                case,
                trial_index=trial_idx,
                use_cache=True,  # safe to resume mid-eval
                write_trajectory=False,  # save disk space across many trials
                inter_case_sleep_s=inter_case_sleep_s,
            )
            all_results[case.name].append(result)

    cases_stats = [_aggregate(c.name, all_results[c.name]) for c in cases]
    total_wall_s = time.perf_counter() - t0
    out_path = _write_report(version, cases_stats, started_at, total_wall_s)
    print(f"\n\n→ Eval report: {out_path}")
    print(f"→ Total wall-clock: {total_wall_s / 60:.1f} min")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--version", "-v", required=True,
        help="Version label for the report (e.g. v1.0, v1.1)",
    )
    p.add_argument(
        "--trials", "-t", type=int, default=3,
        help="Trials per case (default: 3)",
    )
    p.add_argument(
        "--cases", "-c", nargs="+", default=list(_DEFAULT_CORE_CASES),
        help=f"Case names (default: {' '.join(_DEFAULT_CORE_CASES)})",
    )
    p.add_argument(
        "--inter-case-sleep-s", type=float, default=60.0,
        help="Pause between cases for OpenAI TPM refill (default: 60s, 0 to disable)",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(_run_eval(
        version=args.version,
        case_names=args.cases,
        trials=args.trials,
        inter_case_sleep_s=args.inter_case_sleep_s,
    ))


if __name__ == "__main__":
    main()
