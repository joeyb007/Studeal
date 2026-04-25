"""
Smoke test: runs the orchestrator on a single keyword and prints results
with a full URL resolution eval summary.

Usage:
    source venv/bin/activate
    python smoke_test.py "Sony WH-1000XM4"
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Resolution path tracker — hooks into the orchestrator's debug log output
# ---------------------------------------------------------------------------

class ResolutionTracker(logging.Handler):
    """Captures per-deal resolution path from orchestrator DEBUG logs."""

    def __init__(self) -> None:
        super().__init__()
        # title[:40] → path label
        self.paths: dict[str, str] = {}

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "attached label[" in msg:
            title = _extract_title(msg, "to ")
            self.paths[title] = "primary (listing_index)"
        elif "fallback-A matched" in msg:
            title = _extract_title(msg, "matched ")
            self.paths[title] = "fallback-A (merchant+price)"
        elif "fallback-B matched" in msg:
            title = _extract_title(msg, "matched ")
            self.paths[title] = "fallback-B (token overlap)"
        elif "no label found for" in msg:
            title = _extract_title(msg, "for ")
            self.paths[title] = "unresolved (no label)"
        elif "missing identity for" in msg:
            title = _extract_title(msg, "for ")
            self.paths[title] = "skipped (missing identity)"


def _extract_title(msg: str, after: str) -> str:
    """Pull the title substring that appears after `after` in the log message."""
    try:
        part = msg.split(after, 1)[1]
        # strip surrounding quotes/whitespace
        return part.strip().strip("'\"")
    except IndexError:
        return ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(keyword: str) -> None:
    backend = os.environ.get("LLM_BACKEND", "ollama")
    print(f"\nBackend : {backend}")
    print(f"Keyword : {keyword!r}")
    print("=" * 70)

    # Attach tracker before importing orchestrator so it catches all logs
    tracker = ResolutionTracker()
    tracker.setLevel(logging.DEBUG)
    orch_logger = logging.getLogger("dealbot.agents.orchestrator")
    orch_logger.addHandler(tracker)
    orch_logger.setLevel(logging.DEBUG)

    # Suppress noisy lower-level loggers in normal output
    logging.basicConfig(level=logging.WARNING)

    if backend == "openai":
        from dealbot.llm.openai_client import OpenAIClient
        llm = OpenAIClient()
    elif backend == "groq":
        from dealbot.llm.groq_client import GroqClient
        llm = GroqClient()
    elif backend == "vllm":
        from dealbot.llm.vllm import vLLMClient
        llm = vLLMClient()
    else:
        from dealbot.llm.ollama import OllamaClient
        llm = OllamaClient()

    from dealbot.agents.orchestrator import OrchestratorAgent
    agent = OrchestratorAgent(llm=llm)

    print("Running orchestrator...\n")
    candidates = await agent.run(keyword)

    if not candidates:
        print("No candidates found.")
        return

    # ---------------------------------------------------------------------------
    # Categorise each deal
    # ---------------------------------------------------------------------------
    featured: list = []
    organic_resolved: list = []
    organic_label_miss: list = []   # had label, find_url returned ""
    organic_no_label: list = []     # no raw_button_label attached

    for deal in candidates:
        has_real_url = "google.com" not in deal.url
        is_organic = deal.raw_button_label is not None or (
            not has_real_url and deal.raw_button_label is None
        )

        if deal.raw_button_label is None and has_real_url:
            featured.append(deal)
        elif deal.raw_button_label is not None and has_real_url:
            organic_resolved.append(deal)
        elif deal.raw_button_label is not None and not has_real_url:
            organic_label_miss.append(deal)
        else:
            organic_no_label.append(deal)

    total = len(candidates)
    total_organic = len(organic_resolved) + len(organic_label_miss) + len(organic_no_label)
    resolved_count = len(organic_resolved)
    resolution_rate = resolved_count / total_organic * 100 if total_organic else 0

    # ---------------------------------------------------------------------------
    # Print deal table
    # ---------------------------------------------------------------------------
    print(f"{'#':<4} {'STATUS':<10} {'MERCHANT':<22} {'PRICE':>8}  {'DISC':>5}  TITLE")
    print("-" * 70)

    for i, deal in enumerate(candidates, 1):
        has_real_url = "google.com" not in deal.url
        if deal.raw_button_label is None and has_real_url:
            status = "featured"
        elif has_real_url:
            status = "resolved"
        elif deal.raw_button_label is not None:
            status = "?? miss"
        else:
            status = "?? skip"

        discount = (
            f"{(deal.listed_price - deal.sale_price) / deal.listed_price * 100:.0f}%"
            if deal.listed_price > deal.sale_price
            else "—"
        )
        title = deal.title[:38] + ("…" if len(deal.title) > 38 else "")
        merchant = deal.source[:20] + ("…" if len(deal.source) > 20 else "")
        print(f"{i:<4} {status:<10} {merchant:<22} ${deal.sale_price:>7.2f}  {discount:>5}  {title}")

    # ---------------------------------------------------------------------------
    # Resolution eval summary
    # ---------------------------------------------------------------------------
    print()
    print("=" * 70)
    print("URL RESOLUTION EVAL SUMMARY")
    print("=" * 70)
    print(f"  Total deals found    : {total}")
    print(f"  Featured (direct URL): {len(featured)}")
    print(f"  Organic total        : {total_organic}")
    print(f"  Organic resolved     : {resolved_count}  ✅")
    print(f"  Organic label miss   : {len(organic_label_miss)}  ⚠️  (label attached, find_url returned empty)")
    print(f"  Organic no label     : {len(organic_no_label)}  ❌ (no button label attached)")
    print(f"  Resolution rate      : {resolved_count}/{total_organic} = {resolution_rate:.0f}%")
    print()

    # Resolution path breakdown
    if tracker.paths:
        path_counts: dict[str, int] = defaultdict(int)
        for p in tracker.paths.values():
            path_counts[p] += 1
        print("  Resolution paths:")
        for path, count in sorted(path_counts.items(), key=lambda x: -x[1]):
            print(f"    {count:>3}x  {path}")
        print()

    # ---------------------------------------------------------------------------
    # Best deals by discount
    # ---------------------------------------------------------------------------
    discounted = [
        d for d in candidates
        if d.listed_price > d.sale_price and "google.com" not in d.url
    ]
    discounted.sort(key=lambda d: (d.listed_price - d.sale_price) / d.listed_price, reverse=True)

    if discounted:
        print("  Top resolved deals by discount:")
        for d in discounted[:5]:
            pct = (d.listed_price - d.sale_price) / d.listed_price * 100
            url_short = d.url[:60] + ("…" if len(d.url) > 60 else "")
            print(f"    {pct:.0f}% off  ${d.sale_price:.2f}  [{d.source}] {d.title[:40]}")
            print(f"           {url_short}")
        print()

    print("=" * 70)


if __name__ == "__main__":
    keyword = sys.argv[1] if len(sys.argv) > 1 else "mechanical keyboard"
    asyncio.run(main(keyword))
