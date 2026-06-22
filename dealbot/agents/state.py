"""Pydantic state models for the browser agent.

This module defines every piece of state the orchestrator and its workers
read or write. The same models are serialized into the trajectory DB at
run end, so they double as the on-the-wire format for the trajectory
viewer.

Naming convention: top-level state object is `OrchestratorState`. Every
field in it is also a Pydantic model defined here. No bare dicts crossing
module boundaries.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from dealbot.schemas import WatchlistContext


# ---------------------------------------------------------------------------
# Provenance — every finding & every offer field is tagged with where the
# data came from. "observation" means a human-equivalent (the agent literally
# saw it on a page). "inference" means derived via reasoning. Validator
# enforces observation-only for the final ranking.
# ---------------------------------------------------------------------------

Provenance = Literal["instruction", "observation", "inference"]


class Finding(BaseModel):
    """A fact extracted during a PageReader dispatch."""

    text: str
    provenance: Provenance
    source_url: str | None = None


class DealOffer(BaseModel):
    """A candidate deal offered to the user. Provenance-tagged per field
    because price and URL are the load-bearing ones."""

    title: str
    title_provenance: Provenance = "observation"
    price: float
    price_provenance: Provenance
    listed_price: float | None = None
    listed_price_provenance: Provenance | None = None
    url: str
    url_provenance: Provenance
    retailer: str
    condition: str = "unknown"
    condition_provenance: Provenance = "observation"


# ---------------------------------------------------------------------------
# Thread — one lead the agent is exploring. The frontier and parked stacks
# both hold these.
# ---------------------------------------------------------------------------

class Thread(BaseModel):
    id: str
    parent_id: str | None = None
    intent: str
    current_url: str | None = None
    findings: list[Finding] = Field(default_factory=list)
    visited_urls: list[str] = Field(default_factory=list)
    # Leaf URLs the agent has already extracted offers from. PageReader is
    # told not to click into these again; this lets the agent go deeper on
    # the same search results page instead of re-extracting the same items.
    extracted_leaf_urls: list[str] = Field(default_factory=list)
    # Number of findings present when offer_extractor last ran successfully
    # on this thread. The orchestrator's forced-extraction guardrail re-fires
    # when len(findings) - this >= 3, so growing exploration translates into
    # additional offer extractions instead of wasted PageReader work.
    findings_at_last_extraction: int = 0
    # Number of PageReader dispatches in a row that returned 0 findings.
    # When this hits the exhaustion threshold (3), the orchestrator skips
    # this thread when picking a dispatch target — preventing the "agent
    # scrolls an exhausted Kijiji search results page 19× in a row"
    # pathology surfaced by spike trace inspection.
    consecutive_empty_dispatches: int = 0
    # Number of times offer_extractor has errored on this thread. After
    # the cap, the forced-extraction guardrail stops re-firing on it —
    # prevents the infinite-retry-loop pathology where a malformed LLM
    # output causes 18+ wasted offer_extractor calls per run.
    failed_extractions: int = 0
    depth: int = 0
    estimated_value: float = 0.5    # set by LeadScorer when spawned
    last_explored_at: int = 0       # turn number


# ---------------------------------------------------------------------------
# Sufficiency — deterministic stop gate. The orchestrator's `stop` decision
# is only honored when can_stop() is True OR hard budget is exhausted.
# ---------------------------------------------------------------------------

class SufficiencyState(BaseModel):
    distinct_domains_visited: int = 0
    has_price_baseline: bool = False
    offer_count: int = 0
    turns_since_offer_improvement: int = 0

    def can_stop(self) -> bool:
        return (
            self.distinct_domains_visited >= 3
            and self.offer_count >= 3
            and self.turns_since_offer_improvement >= 5
        )


# ---------------------------------------------------------------------------
# Folded memory — AgentFold-style multi-scale summaries. The orchestrator's
# prompt sees a compact form of past turns, never the raw history.
# ---------------------------------------------------------------------------

class FoldedBlock(BaseModel):
    summary: str
    turn_range: tuple[int, int]
    scale: Literal["fine", "coarse"]


class MultiScaleSummary(BaseModel):
    long_term: list[FoldedBlock] = Field(default_factory=list)   # coarse, completed sub-tasks
    recent: list[FoldedBlock] = Field(default_factory=list)      # fine, last 3-5 steps
    raw_latest: str = ""                                          # full-fidelity most recent observation


class FoldingDirective(BaseModel):
    """The orchestrator emits one of these per turn alongside its worker
    decision. Tells the orchestrator how to compress past steps."""

    type: Literal["granular_condense", "deep_consolidate", "none"]
    target_steps: list[int] | None = None
    new_summary: str | None = None


# ---------------------------------------------------------------------------
# Action memory — failed actions per URL. Surfaced to PageReader at the
# start of each dispatch so it doesn't repeat them.
# ---------------------------------------------------------------------------

class FailedAction(BaseModel):
    tool: str
    args_summary: str           # short string, not the raw JSON
    error_type: Literal["timeout", "not_found", "detached", "blocked", "denylist"]
    turn: int


# ---------------------------------------------------------------------------
# Vision fallback log — `take_screenshot` is a stub in v1. Every invocation
# is logged here so we know which URLs would benefit from a real VLM.
# ---------------------------------------------------------------------------

class VisionFallbackEntry(BaseModel):
    url: str
    reason: str
    turn: int


# ---------------------------------------------------------------------------
# Step record — one entry per orchestrator decision. Accumulates during the
# run, batched into the trajectory_steps table at completion.
# ---------------------------------------------------------------------------

class ToolCallRecord(BaseModel):
    """A single tool invocation inside a PageReader sub-trace."""

    tool: str
    args_summary: str
    result_summary: str
    duration_ms: int
    error: str | None = None


class StepRecord(BaseModel):
    turn: int
    worker: str                                 # "search_planner" | "page_reader" | ...
    args_summary: str
    result_summary: str
    cost_usd: float
    duration_ms: int
    folding_directive: FoldingDirective | None = None
    sub_trace: list[ToolCallRecord] | None = None   # populated only for PageReader


# ---------------------------------------------------------------------------
# Orchestrator decision — the LLM's per-turn output.
# ---------------------------------------------------------------------------

class OrchestratorDecision(BaseModel):
    reasoning: str
    worker: Literal[
        "search_planner", "page_reader", "lead_scorer",
        "offer_extractor", "validator", "stop",
    ]
    args: dict[str, Any] = Field(default_factory=dict)
    # The folding directive the LLM emits alongside its worker pick.
    # Stored as a raw dict to avoid coupling — the orchestrator's
    # _apply_folding() interprets it defensively.
    folding_directive: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# The big one — all the state, in one place.
# ---------------------------------------------------------------------------

class OrchestratorState(BaseModel):
    """Mutated by the orchestrator across the run. Persisted at run end."""

    # Input — invariant, used as the goal anchor in every orchestrator prompt.
    spec: WatchlistContext

    # Frontier / parked / current — thread management.
    frontier: list[Thread] = Field(default_factory=list)     # sorted by est_value desc
    parked: list[Thread] = Field(default_factory=list)       # LIFO stack
    current_thread: Thread | None = None

    # Accumulating output.
    offers: list[DealOffer] = Field(default_factory=list)

    # Memory + meta.
    multi_scale_summary: MultiScaleSummary = Field(default_factory=MultiScaleSummary)
    action_memory: dict[str, list[FailedAction]] = Field(default_factory=dict)
    vision_fallback_log: list[VisionFallbackEntry] = Field(default_factory=list)

    # Budget + progress.
    turn: int = 0
    cost_usd: float = 0.0
    sufficiency: SufficiencyState = Field(default_factory=SufficiencyState)
    consecutive_no_progress: int = 0

    # Trajectory accumulator — flushed to DB at run completion.
    history: list[StepRecord] = Field(default_factory=list)
