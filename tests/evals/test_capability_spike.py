"""Capability spike — runs the current agent against 5 real marketplace
searches. Exploratory; assertions are minimal. The point is to find out
whether the agent works on real pages, not to validate a contract.

Run via:
  ./venv/bin/pytest tests/evals/test_capability_spike.py -s -v

Bypasses SearchPlanner by injecting a fixture planner that returns a
hardcoded marketplace search URL. Isolates "does perception + extraction
work" from "does the LLM generate good URLs."
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv

# Load .env early so GROQ_API_KEY etc. are available at import time.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from dealbot.agents.composition import build_eval_orchestrator
from dealbot.agents.state import OrchestratorState, Thread
from dealbot.agents.workers.search_planner import SearchPlanner
from dealbot.llm.base import LLMClient
from dealbot.llm.openai_client import OpenAIClient
from dealbot.schemas import WatchlistContext
from dealbot.scrapers.browser_session import LocalPlaywrightSession

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Test matrix
# ---------------------------------------------------------------------------


@dataclass
class SpikeCase:
    name: str
    marketplace: str
    query: str
    start_url: str
    geo: str
    max_budget: float | None = None
    # Optional path to a Playwright storage_state file (cookies + localStorage)
    # for sites that need a logged-in session. Produced by fb_auth_helper.py.
    storage_state: str | None = None
    # "fixed" = start at start_url directly. "google" = start at a Google
    # search and let the agent navigate to the right site itself (real
    # autonomy test).
    entry_mode: str = "fixed"


_FB_STATE = str(Path(__file__).resolve().parent / "_fb_state.json")


def _google(query: str) -> str:
    """Build a Google search URL for the autonomy-test entry mode."""
    from urllib.parse import quote_plus
    return f"https://www.google.com/search?q={quote_plus(query)}"


CASES: list[SpikeCase] = [
    # ---- Fixed-URL mode: isolates perception/extraction ----
    SpikeCase(
        name="kijiji_gta_aeron_fixed",
        marketplace="Kijiji",
        query="Herman Miller Aeron Toronto used",
        start_url="https://www.kijiji.ca/b-toronto-gta/herman-miller-aeron/k0l1700273",
        geo="Toronto GTA",
        max_budget=900.0,
    ),
    SpikeCase(
        name="ebay_ca_aeron_fixed",
        marketplace="eBay Canada",
        query="Herman Miller Aeron used",
        start_url="https://www.ebay.ca/sch/i.html?_nkw=herman+miller+aeron&LH_ItemCondition=3000",
        geo="Canada",
        max_budget=900.0,
    ),
    SpikeCase(
        name="fb_marketplace_toronto_aeron_fixed",
        marketplace="FB Marketplace",
        query="Herman Miller Aeron",
        start_url="https://www.facebook.com/marketplace/toronto/search?query=herman%20miller%20aeron",
        geo="Toronto",
        max_budget=900.0,
        storage_state=_FB_STATE,
    ),
    SpikeCase(
        name="apple_refurb_ca_macbook_fixed",
        marketplace="Apple Refurbished CA",
        query="refurbished MacBook Air M2",
        start_url="https://www.apple.com/ca/shop/refurbished/mac/macbook-air",
        geo="Canada",
        max_budget=1200.0,
    ),
    # ---- Google-source mode: the real autonomy test ----
    SpikeCase(
        name="aeron_google_freeroam",
        marketplace="(freeroam)",
        query="Herman Miller Aeron Toronto used under $700",
        start_url=_google("Herman Miller Aeron Toronto used under $700"),
        geo="Toronto",
        max_budget=700.0,
        entry_mode="google",
    ),
    SpikeCase(
        name="macbook_refurb_google_freeroam",
        marketplace="(freeroam)",
        query="MacBook Air M2 refurbished Canada best price",
        start_url=_google("MacBook Air M2 refurbished Canada best price"),
        geo="Canada",
        max_budget=1200.0,
        entry_mode="google",
        # Reuse FB state so freeroam can land on FB Marketplace too if it
        # discovers it via Google.
        storage_state=_FB_STATE,
    ),
    SpikeCase(
        name="road_bike_google_freeroam",
        marketplace="(freeroam)",
        query="road bike Toronto used Kijiji Facebook",
        start_url=_google("road bike Toronto used Kijiji Facebook"),
        geo="Toronto",
        max_budget=1500.0,
        entry_mode="google",
        storage_state=_FB_STATE,
    ),
    SpikeCase(
        name="oled_tv_outlet_google_freeroam",
        marketplace="(freeroam)",
        query="55 inch OLED TV Best Buy Canada outlet sale",
        start_url=_google("55 inch OLED TV Best Buy Canada outlet sale"),
        geo="Canada",
        max_budget=1500.0,
        entry_mode="google",
    ),
]


# ---------------------------------------------------------------------------
# Fixture SearchPlanner: bypasses LLM URL generation
# ---------------------------------------------------------------------------


class FixtureSearchPlanner(SearchPlanner):
    """Returns one hardcoded starting thread. Isolates perception/extraction
    capability from LLM URL-generation capability.

    Memoizes the thread so repeated calls return the SAME thread (same uuid).
    Without this, the orchestrator's over-calling of search_planner generates
    fresh threads on every turn, and findings get scattered across many
    abandoned threads instead of accumulating on one.
    """

    def __init__(self, llm: LLMClient, start_url: str, intent: str) -> None:
        super().__init__(llm)
        self._start_url = start_url
        self._intent = intent
        self._thread: Thread | None = None

    async def plan(
        self,
        spec: WatchlistContext,
        prior_findings: list[str] | None = None,
    ) -> list[Thread]:
        if self._thread is None:
            self._thread = Thread(
                id=str(uuid.uuid4()),
                intent=self._intent,
                current_url=self._start_url,
                depth=0,
                estimated_value=0.9,
            )
        return [self._thread]


# ---------------------------------------------------------------------------
# Result capture
# ---------------------------------------------------------------------------


@dataclass
class SpikeResult:
    case_name: str
    marketplace: str
    query: str
    start_url: str
    completed: bool = False
    error: str | None = None
    listing_count: int = 0
    turn_count: int = 0
    cost_usd: float = 0.0
    latency_sec: float = 0.0
    domains_visited: int = 0
    vision_fallbacks: int = 0
    stop_reason: str = ""
    listings: list[dict[str, Any]] = field(default_factory=list)


# Module-level results store. We write a per-case JSON sidecar after each
# case so a kill mid-run doesn't lose completed cases. The session-scoped
# finalizer aggregates all sidecars (including past runs') into the
# summary markdown.
_RESULTS: list[SpikeResult] = []

_CASES_DIR = Path(__file__).resolve().parents[2] / "docs" / "spike-cases"


def _sidecar_path(case_name: str) -> Path:
    return _CASES_DIR / f"{case_name}.json"


def _save_sidecar(result: SpikeResult) -> None:
    _CASES_DIR.mkdir(parents=True, exist_ok=True)
    _sidecar_path(result.case_name).write_text(
        json.dumps(result.__dict__, indent=2, default=str)
    )


def _save_history(case_name: str, state: OrchestratorState) -> None:
    """Dump the full orchestrator trajectory for diagnostic inspection."""
    _CASES_DIR.mkdir(parents=True, exist_ok=True)
    history_path = _CASES_DIR / f"{case_name}_history.json"
    payload = {
        "spec": state.spec.model_dump(mode="json"),
        "final_offers_count": len(state.offers),
        "final_turn": state.turn,
        "final_cost_usd": state.cost_usd,
        "sufficiency": state.sufficiency.model_dump(mode="json"),
        "action_memory": {
            url: [m.model_dump(mode="json") for m in mems]
            for url, mems in state.action_memory.items()
        },
        "vision_fallback_log": [
            e.model_dump(mode="json") for e in state.vision_fallback_log
        ],
        "history": [step.model_dump(mode="json") for step in state.history],
    }
    history_path.write_text(json.dumps(payload, indent=2, default=str))


def _load_sidecar(case_name: str) -> SpikeResult | None:
    p = _sidecar_path(case_name)
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    return SpikeResult(**data)


def _stop_reason(state: OrchestratorState) -> str:
    if state.sufficiency.can_stop():
        return "sufficiency_met"
    if state.cost_usd > 0:
        return "budget_or_turns_exhausted"
    return "turns_exhausted"


# ---------------------------------------------------------------------------
# The spike
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _emit_summary_at_end():
    """Session-scoped: emit a markdown summary at the end no matter what."""
    yield
    if not _RESULTS:
        return

    repo_root = Path(__file__).resolve().parents[2]
    out_path = repo_root / "docs" / "spike-results.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = ["# Capability Spike Results", ""]
    lines.append("Generated by `tests/evals/test_capability_spike.py`.")
    lines.append("")
    lines.append("## Pass/fail table")
    lines.append("")
    lines.append("| # | Case | Marketplace | Completed | Listings | Turns | Latency | Stop reason |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(_RESULTS, 1):
        completed = "✅" if r.completed and r.error is None else "❌"
        lines.append(
            f"| {i} | {r.case_name} | {r.marketplace} | {completed} | "
            f"{r.listing_count} | {r.turn_count} | {r.latency_sec:.1f}s | "
            f"{r.stop_reason or '-'} |"
        )

    lines.extend(["", "## Per-case details", ""])
    for r in _RESULTS:
        lines.append(f"### {r.case_name}")
        lines.append(f"- Query: `{r.query}`")
        lines.append(f"- Start URL: {r.start_url}")
        lines.append(f"- Marketplace: {r.marketplace}")
        lines.append(f"- Completed: {r.completed}  Error: {r.error or 'none'}")
        lines.append(
            f"- Listings: {r.listing_count}  Turns: {r.turn_count}  "
            f"Domains: {r.domains_visited}  Vision-fallbacks: {r.vision_fallbacks}"
        )
        lines.append(f"- Cost: ${r.cost_usd:.4f}  Latency: {r.latency_sec:.1f}s")
        lines.append(f"- Stop reason: {r.stop_reason or '-'}")
        if r.listings:
            lines.append("")
            lines.append("Listings extracted:")
            for j, ll in enumerate(r.listings, 1):
                lines.append(
                    f"  {j}. **{ll.get('title', '?')[:80]}** — "
                    f"${ll.get('price', '?')} — "
                    f"`{ll.get('url', '?')[:100]}` — "
                    f"condition: {ll.get('condition', '?')}"
                )
        lines.append("")

    lines.extend([
        "## Recommendation",
        "",
        "_(Manually fill this in after reviewing the table above. Decision tree per v12 plan:_",
        "_ ≥4/5 → write tight v13. 2-3/5 → write v13 against specific fixes. ≤1/5 → escalate.)_",
        "",
    ])

    out_path.write_text("\n".join(lines))
    print(f"\n\n→ Spike results written to {out_path}\n")


async def run_spike_case(
    case: SpikeCase,
    *,
    trial_index: int | None = None,
    use_cache: bool = True,
    write_trajectory: bool = True,
    inter_case_sleep_s: float | None = None,
) -> SpikeResult:
    """Run one spike case end-to-end and return a SpikeResult.

    Extracted from the pytest wrapper so the eval framework
    (scripts/run_evals.py) can call it directly in a multi-trial loop
    without going through pytest.

    - `trial_index`: when given, sidecar path becomes `<name>_t<N>.json`
      so different trials don't overwrite each other. Default (None) uses
      the legacy `<name>.json` path for backward compatibility.
    - `use_cache`: if True, returns a cached sidecar when one exists.
      Eval runs typically pass False to force fresh trials.
    - `write_trajectory`: if False, skips the full history JSON dump
      (the report.md from the trace writer is still produced).
    - `inter_case_sleep_s`: post-run pause for OpenAI TPM refill. None
      = read from SPIKE_INTER_CASE_SLEEP env var. Set 0 to disable.
    """
    sidecar_name = (
        case.name if trial_index is None
        else f"{case.name}_t{trial_index:02d}"
    )

    if use_cache:
        cached = _load_sidecar(sidecar_name)
        if cached is not None:
            print(f"\n=== {sidecar_name} (cached) ===")
            print(f"    listings={cached.listing_count} turns={cached.turn_count} "
                  f"latency={cached.latency_sec:.1f}s stop={cached.stop_reason}")
            return cached

    result = SpikeResult(
        case_name=sidecar_name,
        marketplace=case.marketplace,
        query=case.query,
        start_url=case.start_url,
    )

    trial_tag = "" if trial_index is None else f" (trial {trial_index})"
    print(f"\n\n=== {case.name}{trial_tag} ===")
    print(f"    {case.marketplace} | query={case.query!r} | geo={case.geo}")
    print(f"    Starting at: {case.start_url}")

    t0 = time.perf_counter()
    try:
        # gpt-4o-mini default — cheap enough for a spike, capable enough
        # to test the architecture. If extraction quality is weak we can
        # rerun with OPENAI_MODEL=gpt-4o.
        llm = OpenAIClient()
        # Headed unless SPIKE_HEADLESS=1 — so you can watch the agent work.
        headless = os.environ.get("SPIKE_HEADLESS", "0") == "1"
        case_storage = case.storage_state
        orchestrator = build_eval_orchestrator(
            orchestrator_llm=llm,
            session_factory=lambda: LocalPlaywrightSession(
                headless=headless, storage_state=case_storage,
            ),
        )
        orchestrator.max_turns = int(os.environ.get("SPIKE_MAX_TURNS", "25"))
        orchestrator.search_planner = FixtureSearchPlanner(
            llm=llm,
            start_url=case.start_url,
            intent=f"Find used {case.query} listings on {case.marketplace}",
        )

        # Observability: per-trial trace dir so multi-trial runs don't
        # overwrite each other's traces.
        if os.environ.get("SPIKE_NO_TRACE") != "1":
            from dealbot.agents.tracing import FilesystemTraceWriter
            trace_root = (
                Path(__file__).resolve().parents[2]
                / "docs" / "spike-traces" / sidecar_name
            )
            if trace_root.exists():
                import shutil
                shutil.rmtree(trace_root)
            trace_writer = FilesystemTraceWriter(trace_root, run_label=sidecar_name)
            orchestrator.trace_writer = trace_writer
            orchestrator.page_reader.trace_writer = trace_writer

        spec = WatchlistContext(
            product_query=f"{case.query} (used/secondhand) {case.geo}",
            max_budget=case.max_budget,
        )

        state = await orchestrator.run(spec)
        result.latency_sec = time.perf_counter() - t0
        result.completed = True
        result.listing_count = len(state.offers)
        result.turn_count = state.turn
        result.cost_usd = state.cost_usd
        result.domains_visited = state.sufficiency.distinct_domains_visited
        result.vision_fallbacks = len(state.vision_fallback_log)
        result.stop_reason = _stop_reason(state)
        result.listings = [
            {
                "title": o.title,
                "price": o.price,
                "url": o.url,
                "retailer": o.retailer,
                "condition": o.condition,
            }
            for o in state.offers
        ]

        if write_trajectory:
            try:
                _save_history(sidecar_name, state)
            except Exception as exc:
                logger.warning("trajectory save failed: %s", exc)

        print(f"\n    → completed in {result.latency_sec:.1f}s")
        print(f"    → {result.listing_count} listings, {result.turn_count} turns, "
              f"${result.cost_usd:.4f}, stop={result.stop_reason}")

    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.latency_sec = time.perf_counter() - t0
        print(f"\n    → CRASHED: {result.error}")
        logger.exception("spike case %s%s crashed", case.name, trial_tag)
    finally:
        # Save sidecar under per-trial name so resume works correctly.
        result.case_name = sidecar_name
        try:
            _save_sidecar(result)
        except Exception as exc:
            logger.warning("sidecar save failed for %s: %s", sidecar_name, exc)

    # Inter-case pause for OpenAI TPM refill.
    sleep_s = (
        inter_case_sleep_s
        if inter_case_sleep_s is not None
        else float(os.environ.get("SPIKE_INTER_CASE_SLEEP", "60"))
    )
    if sleep_s > 0:
        print(f"    → sleeping {sleep_s:.0f}s for OpenAI TPM refill...")
        await asyncio.sleep(sleep_s)

    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
async def test_capability_spike(case: SpikeCase) -> None:
    """Pytest wrapper around run_spike_case. Single-trial, cached-by-default
    behavior for backward compatibility with the existing spike harness."""
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set; cannot run live spike")

    result = await run_spike_case(case, trial_index=None, use_cache=True)
    _RESULTS.append(result)

    # The spike's only assertion: it ran (crash or not) and was captured.
    assert True
