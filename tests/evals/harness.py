"""Eval harness core — forced-target pipeline driver and result recorder.

This module is a LIBRARY. It has no test_ functions, no __main__ block, and
no side effects at import time. Eval scripts import it; pytest does not collect
it directly.

Per-token rate constants (estimates; update as OpenAI pricing changes):
    gpt-4o input  $2.50 / 1M tokens  →  $0.0000025 / token
    gpt-4o output $10.00 / 1M tokens →  $0.000010  / token
    gpt-4o-mini input  $0.15 / 1M tokens →  $0.00000015 / token
    gpt-4o-mini output $0.60 / 1M tokens →  $0.00000060 / token

Nav model = gpt-4o; extract model = gpt-4o-mini.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dealbot.agents.marketplace_router import (
    CURATED_MARKETPLACES,
    MarketplaceSearchTarget,
)
from dealbot.persistence.canonicalize import canonicalize_url
from dealbot.schemas import WatchlistContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-token rate estimates (USD) — gpt-4o nav, gpt-4o-mini extract
# ---------------------------------------------------------------------------

# Per-model $/token rates (prompt, completion). Estimates for cost accounting.
_MODEL_RATES: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50 / 1_000_000, 10.00 / 1_000_000),
    "gpt-4o-mini": (0.15 / 1_000_000, 0.60 / 1_000_000),
}
_DEFAULT_NAV_MODEL = "gpt-4o"
_DEFAULT_EXTRACT_MODEL = "gpt-4o-mini"

# Back-compat aliases (unused by _estimate_cost now; kept for any importer).
_NAV_PROMPT_RATE, _NAV_COMPLETION_RATE = _MODEL_RATES["gpt-4o"]
_EXTRACT_PROMPT_RATE, _EXTRACT_COMPLETION_RATE = _MODEL_RATES["gpt-4o-mini"]


def _rates_for(model: str) -> tuple[float, float]:
    # Unknown model → gpt-4o rate (conservative: never under-reports cost).
    return _MODEL_RATES.get(model, _MODEL_RATES["gpt-4o"])

# Reliability bar thresholds (binding — matches Global Constraints in the brief)
_BAR_MIN_OFFERS: int = 10
_BAR_MIN_MARKETPLACES: int = 2
_BAR_MAX_WALL_CLOCK_S: float = 240.0

# Home-mode URL overrides: marketplaces where scheme://netloc is not the home
_HOME_URL_OVERRIDES: dict[str, str] = {
    # scheme://netloc of the search URL is the News Feed, not Marketplace.
    "fb_marketplace": "https://www.facebook.com/marketplace/",
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class CaseResult:
    case_id: str
    spec_query: str
    marketplaces: list[str]            # forced targets
    entry_mode: str                    # "template" | "home"
    offers_total: int
    offers_unique: int                 # by canonicalized URL
    offers_by_marketplace: dict[str, int]
    marketplaces_with_listings: int
    wall_clock_s: float
    stop_reasons: dict[str, str]       # "<marketplace>/<query>" → reason
    error_stops: int
    nav_prompt_tokens: int
    nav_completion_tokens: int
    extract_prompt_tokens: int
    extract_completion_tokens: int
    est_cost_usd: float                # fixed per-token rates (module constants)
    passed_bar: bool                   # reliability bar — all four clauses
    precision: float | None = None     # matched/total; None when offers_total == 0


# ---------------------------------------------------------------------------
# Pure helpers (tested offline)
# ---------------------------------------------------------------------------

def _resolve_targets(
    marketplaces: list[str],
    query: str,
    entry_mode: str,
) -> list[MarketplaceSearchTarget]:
    """Build MarketplaceSearchTarget list from forced marketplace names.

    template mode: entry_url = build_search_url(query)
    home mode:     entry_url = scheme + netloc of the search URL (site root),
                   or override if present in _HOME_URL_OVERRIDES

    Raises ValueError for any name not in CURATED_MARKETPLACES.
    """
    by_key = {m.key: m for m in CURATED_MARKETPLACES}
    targets: list[MarketplaceSearchTarget] = []
    for name in marketplaces:
        config = by_key.get(name)
        if config is None:
            raise ValueError(
                f"unknown marketplace {name!r}; "
                f"valid keys: {sorted(by_key)}"
            )
        if entry_mode == "home":
            # Check for explicit override (e.g., fb_marketplace → /marketplace/)
            if name in _HOME_URL_OVERRIDES:
                entry_url = _HOME_URL_OVERRIDES[name]
            else:
                search_url = config.build_search_url("")
                parsed = urlparse(search_url)
                entry_url = f"{parsed.scheme}://{parsed.netloc}"
        else:
            entry_url = config.build_search_url(query)
        targets.append(MarketplaceSearchTarget(
            marketplace=config.key,
            entry_url=entry_url,
        ))
    return targets


def _passed_bar(result: CaseResult) -> bool:
    """Return True iff the result clears all four reliability bar clauses."""
    return _passed_bar_from_values(
        result.offers_total,
        result.marketplaces_with_listings,
        result.wall_clock_s,
        result.error_stops,
    )


def _estimate_cost(
    nav_prompt: int,
    nav_completion: int,
    extract_prompt: int,
    extract_completion: int,
    nav_model: str | None = None,
    extract_model: str | None = None,
) -> float:
    # Model-aware: nav rate must follow the ACTUAL nav model, else a gpt-4o-mini
    # nav run (e.g. an ablation cell) is charged at gpt-4o rates (~17x over).
    nav_p, nav_c = _rates_for(
        nav_model or os.environ.get("AGENT_NAV_MODEL", _DEFAULT_NAV_MODEL)
    )
    ext_p, ext_c = _rates_for(
        extract_model or os.environ.get("OPENAI_MODEL", _DEFAULT_EXTRACT_MODEL)
    )
    return (
        nav_prompt * nav_p
        + nav_completion * nav_c
        + extract_prompt * ext_p
        + extract_completion * ext_c
    )


# Stopwords excluded from product-query token matching
_QUERY_STOPWORDS: frozenset[str] = frozenset({"the", "a", "an", "for", "in", "of"})


def offer_matches_spec(offer: object, spec: "WatchlistContext") -> bool:
    """Heuristic precision check: does this offer plausibly match the spec?

    Rules (all must pass):
    1. offer.title contains >=1 brand token (case-insensitive substring match on
       each whitespace-split token from spec.brands).
    2. offer.title contains >=1 product-query token from spec.product_query,
       excluding stopwords and any token already counted as a brand token.
    3. If offer.price and spec.max_budget are both set (non-None, non-zero),
       offer.price must be <= spec.max_budget * 1.2.
    """
    title_lower: str = (getattr(offer, "title", "") or "").lower()

    # Collect brand tokens (lowercased, whitespace-split)
    brands: list[str] = getattr(spec, "brands", None) or []
    brand_tokens: list[str] = []
    for brand in brands:
        brand_tokens.extend(brand.lower().split())

    # Rule 1: title must contain at least one brand token
    matched_brand_tokens: set[str] = set()
    for bt in brand_tokens:
        if bt and bt in title_lower:
            matched_brand_tokens.add(bt)
    if not matched_brand_tokens:
        return False

    # Rule 2: title must contain at least one product-query token (not a brand
    # token, not a stopword)
    product_query: str = getattr(spec, "product_query", "") or ""
    # Exclude MATCHED brand tokens (found in title), not all declared brand tokens,
    # so an unmatched brand token can still count as a query token.
    query_tokens: list[str] = [
        t for t in product_query.lower().split()
        if t not in _QUERY_STOPWORDS and t not in matched_brand_tokens
    ]
    found_query_token = any(qt and qt in title_lower for qt in query_tokens)
    if not found_query_token:
        return False

    # Rule 3: price check (both must be set and non-zero)
    offer_price = getattr(offer, "price", None)
    max_budget = getattr(spec, "max_budget", None)
    if offer_price is not None and max_budget is not None:
        if offer_price > max_budget * 1.2:
            return False

    return True


# ---------------------------------------------------------------------------
# Result recording
# ---------------------------------------------------------------------------

_MD_HEADERS = (
    "case_id", "spec_query", "entry_mode", "marketplaces",
    "offers_total", "offers_unique", "marketplaces_with_listings",
    "wall_clock_s", "error_stops", "est_cost_usd", "passed_bar", "precision",
)


def append_result(result: CaseResult, out_dir: str = "docs/evals") -> None:
    """Append one JSONL line to results.jsonl and one markdown row to results.md.

    Creates both files (with headers) if absent.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # --- JSONL ---
    jsonl_path = out / "results.jsonl"
    record = dataclasses.asdict(result)
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")

    # --- Markdown ---
    md_path = out / "results.md"
    needs_header = not md_path.exists() or md_path.stat().st_size == 0
    with md_path.open("a", encoding="utf-8") as f:
        if needs_header:
            f.write("| " + " | ".join(_MD_HEADERS) + " |\n")
            f.write("| " + " | ".join("---" for _ in _MD_HEADERS) + " |\n")
        row_values = []
        for h in _MD_HEADERS:
            val = record.get(h, "")
            # Flatten lists/dicts for readability in the table
            if isinstance(val, (list, dict)):
                val = str(val)
            # Format precision: empty string when None, else f"{value:.2f}"
            if h == "precision":
                val = "" if val is None else f"{val:.2f}"
            row_values.append(str(val))
        f.write("| " + " | ".join(row_values) + " |\n")


# ---------------------------------------------------------------------------
# run_case — drives the real pipeline (NOT covered by offline tests)
# ---------------------------------------------------------------------------

async def run_case(
    case_id: str,
    spec: WatchlistContext,
    marketplaces: list[str],
    entry_mode: str = "template",
    queries: list[str] | None = None,
    trace_root: str = "traces/evals",
) -> CaseResult:
    """Drive the pipeline with forced marketplace targets and record results.

    marketplaces must all be keys in CURATED_MARKETPLACES; unknown name raises
    ValueError immediately (before any network I/O).

    queries=None → QueryGenerator generates from spec.
    queries=[...] → used verbatim (preferred for eval run-to-run comparability).
    """
    # Import heavy deps here so the module is importable in offline tests.
    from datetime import datetime, timezone
    from dealbot.agents.composition import (
        ThrottledLLM,
        build_extract_llm,
        build_nav_llm,
        build_session_from_env,
    )
    from dealbot.agents.explorer import Explorer
    from dealbot.agents.extractor_pool import ExtractorPool
    from dealbot.agents.query_generator import QueryGenerator
    from dealbot.agents.tracing import FilesystemTraceWriter, NullTraceWriter
    from dealbot.agents.workers.extractor import Extractor

    # Validate marketplace names eagerly (pure, raises ValueError on bad name).
    # We call _resolve_targets per query later; this validates names now.
    _resolve_targets(marketplaces, spec.product_query, entry_mode)

    nav_llm = build_nav_llm()
    extract_llm = build_extract_llm()
    extractor = Extractor(extract_llm)
    pool = ExtractorPool(extractor, num_workers=3)

    if queries is None:
        query_gen = QueryGenerator(extract_llm)
        queries = await query_gen.generate(spec)
        logger.info("run_case[%s]: generated %d queries: %s", case_id, len(queries), queries)

    await pool.start()

    stop_reasons: dict[str, str] = {}
    t_start = time.monotonic()

    async def _run_one(query: str) -> None:
        targets = _resolve_targets(marketplaces, query, entry_mode)
        slug = re.sub(r"[^a-z0-9]+", "-", query.lower())[:40]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

        async def sink(snap, marketplace):
            await pool.submit(snap, marketplace, spec)

        try:
            async with build_session_from_env() as session:
                for target in targets:
                    trace_dir = os.environ.get("AGENT_TRACE_DIR", trace_root)
                    if trace_dir:
                        trace = FilesystemTraceWriter(
                            Path(trace_dir) / case_id / f"{stamp}_{slug}" / target.marketplace
                        )
                    else:
                        trace = NullTraceWriter()

                    explorer = Explorer(nav_llm, trace=trace)
                    key = f"{target.marketplace}/{query}"
                    try:
                        result = await explorer.explore(
                            entry_url=target.entry_url,
                            marketplace=target.marketplace,
                            query=query,
                            spec=spec,
                            session=session,
                            sink=sink,
                        )
                        stop_reasons[key] = result.stop_reason
                        logger.info(
                            "run_case[%s]: %s/%s urls=%d turns=%d stop=%s",
                            case_id, target.marketplace, query,
                            len(result.urls_visited), result.turns_used,
                            result.stop_reason,
                        )
                        # Mirror production (_run_one_query): one retry with
                        # the spec's core query when a marketplace reports
                        # no_results on a verbose variant.
                        if (
                            result.stop_reason == "done"
                            and result.done_reason
                            and "no_results" in result.done_reason.lower()
                            and query.strip().lower() != spec.product_query.strip().lower()
                        ):
                            retry_q = spec.product_query
                            retry_targets = _resolve_targets(
                                [target.marketplace], retry_q, entry_mode,
                            )
                            if retry_targets:
                                logger.info(
                                    "run_case[%s]: %s no_results — retry with %r",
                                    case_id, target.marketplace, retry_q,
                                )
                                await Explorer(nav_llm, trace=trace).explore(
                                    entry_url=retry_targets[0].entry_url,
                                    marketplace=target.marketplace,
                                    query=retry_q,
                                    spec=spec,
                                    session=session,
                                    sink=sink,
                                )
                    except Exception:
                        logger.exception(
                            "run_case[%s]: explorer failed on %s/%s",
                            case_id, target.marketplace, query,
                        )
                        stop_reasons[key] = "error"
                    finally:
                        trace.finalize()
        except Exception:
            logger.exception("run_case[%s]: session setup failed for %r", case_id, query)

    await asyncio.gather(*(_run_one(q) for q in queries), return_exceptions=True)

    attributed = await pool.drain_attributed()
    wall_clock_s = time.monotonic() - t_start

    # Deduplicate by canonical URL (per-marketplace canonicalization)
    seen_canonical: set[str] = set()
    unique_offers = []
    for offer, marketplace in attributed:
        canon = canonicalize_url(offer.url, marketplace)
        if canon not in seen_canonical:
            seen_canonical.add(canon)
            unique_offers.append((offer, marketplace))

    offers_by_marketplace: dict[str, int] = {}
    for _, marketplace in attributed:
        offers_by_marketplace[marketplace] = offers_by_marketplace.get(marketplace, 0) + 1

    marketplaces_with_listings = sum(
        1 for count in offers_by_marketplace.values() if count > 0
    )
    error_stops = sum(1 for v in stop_reasons.values() if v == "error")

    # Groq fallback clients expose no usage counters — token fields read 0 there.
    nav_prompt_tokens = getattr(nav_llm, "total_prompt_tokens", 0)
    nav_completion_tokens = getattr(nav_llm, "total_completion_tokens", 0)
    extract_prompt_tokens = getattr(extract_llm, "total_prompt_tokens", 0)
    extract_completion_tokens = getattr(extract_llm, "total_completion_tokens", 0)

    est_cost = _estimate_cost(
        nav_prompt_tokens, nav_completion_tokens,
        extract_prompt_tokens, extract_completion_tokens,
    )

    # Compute precision: fraction of total offers matching the spec heuristic.
    # None when no offers were extracted (avoids division by zero and meaningless 0.0).
    offers_total = len(attributed)
    if offers_total == 0:
        precision: float | None = None
    else:
        matched = sum(1 for offer, _ in attributed if offer_matches_spec(offer, spec))
        precision = matched / offers_total

    result = CaseResult(
        case_id=case_id,
        spec_query=spec.product_query,
        marketplaces=marketplaces,
        entry_mode=entry_mode,
        offers_total=offers_total,
        offers_unique=len(unique_offers),
        offers_by_marketplace=offers_by_marketplace,
        marketplaces_with_listings=marketplaces_with_listings,
        wall_clock_s=wall_clock_s,
        stop_reasons=stop_reasons,
        error_stops=error_stops,
        nav_prompt_tokens=nav_prompt_tokens,
        nav_completion_tokens=nav_completion_tokens,
        extract_prompt_tokens=extract_prompt_tokens,
        extract_completion_tokens=extract_completion_tokens,
        est_cost_usd=est_cost,
        passed_bar=_passed_bar_from_values(
            offers_total=offers_total,
            marketplaces_with_listings=marketplaces_with_listings,
            wall_clock_s=wall_clock_s,
            error_stops=error_stops,
        ),
        precision=precision,
    )
    return result


def _passed_bar_from_values(
    offers_total: int,
    marketplaces_with_listings: int,
    wall_clock_s: float,
    error_stops: int,
) -> bool:
    """Compute passed_bar from raw values (used inside run_case before CaseResult is built)."""
    return (
        offers_total >= _BAR_MIN_OFFERS
        and marketplaces_with_listings >= _BAR_MIN_MARKETPLACES
        and wall_clock_s < _BAR_MAX_WALL_CLOCK_S
        and error_stops == 0
    )
