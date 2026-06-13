"""DealHuntOrchestrator — the strategic LLM controller.

Each turn:
  1. Build a compact state snapshot (NOT raw history) + goal anchor.
  2. Ask the orchestrator LLM which worker to dispatch + a folding directive.
  3. Apply the folding directive to multi_scale_summary.
  4. Dispatch the chosen worker; capture result into StepRecord.
  5. Update sufficiency state (distinct domains, offer count, no-progress turns).
  6. Loop until: stop decision (sufficiency-gated) OR max_turns OR cost cap.

Owns:
  - Strategic decisions (worker dispatch)
  - Folding (deep_consolidate at threshold=5)
  - Goal anchor injection
  - Sufficiency gating — `stop` is only honored when can_stop() is True
  - action_memory writes happen inside PageReader; orchestrator just routes
  - Replan loop — Validator can return suggested_leads at most once
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlparse

from pydantic import BaseModel, ValidationError

from dealbot.agents.prompts import ORCHESTRATOR_SYSTEM, render_spec_summary
from dealbot.agents.state import (
    FoldedBlock,
    FoldingDirective,
    MultiScaleSummary,
    OrchestratorDecision,
    OrchestratorState,
    StepRecord,
    Thread,
)
from dealbot.agents.tools import DomainRateLimiter
from dealbot.agents.workers import (
    LeadScorer,
    OfferExtractor,
    PageReader,
    SearchPlanner,
    Validator,
)
from dealbot.llm.base import LLMClient
from dealbot.scrapers.browser_session import BrowserSession
from dealbot.schemas import WatchlistContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration knobs (deterministic; tunable per design doc)
# ---------------------------------------------------------------------------

_MAX_TURNS = 80
_MAX_COST_USD = 0.50
_MAX_REPLANS = 1
_DEEP_CONSOLIDATE_THRESHOLD = 5
_FRONTIER_PROMPT_TOP_N = 5
_RECENT_HISTORY_PROMPT_N = 5


# ---------------------------------------------------------------------------
# LLM response shape
# ---------------------------------------------------------------------------

class _FoldingDirectiveJSON(BaseModel):
    type: str   # "granular_condense" | "deep_consolidate" | "none"
    target_steps: list[int] | None = None
    new_summary: str | None = None


class _DecisionJSON(BaseModel):
    reasoning: str
    folding_directive: _FoldingDirectiveJSON
    worker: str
    args: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

SessionFactory = Callable[[], BrowserSession]


class DealHuntOrchestrator:
    def __init__(
        self,
        *,
        orchestrator_llm: LLMClient,
        search_planner: SearchPlanner,
        page_reader: PageReader,
        lead_scorer: LeadScorer,
        offer_extractor: OfferExtractor,
        validator: Validator,
        session_factory: SessionFactory,
        rate_limiter: DomainRateLimiter | None = None,
        max_turns: int = _MAX_TURNS,
        max_cost_usd: float = _MAX_COST_USD,
        max_replans: int = _MAX_REPLANS,
    ) -> None:
        self.llm = orchestrator_llm
        self.search_planner = search_planner
        self.page_reader = page_reader
        self.lead_scorer = lead_scorer
        self.offer_extractor = offer_extractor
        self.validator = validator
        self.session_factory = session_factory
        self.rate_limiter = rate_limiter or DomainRateLimiter()
        self.max_turns = max_turns
        self.max_cost_usd = max_cost_usd
        self.max_replans = max_replans

    # ---------------------------------------------------------------------
    # Public entrypoint
    # ---------------------------------------------------------------------

    async def run(self, spec: WatchlistContext) -> OrchestratorState:
        state = OrchestratorState(spec=spec)
        replans_used = 0
        last_offer_count = 0

        async with self.session_factory() as session:
            while state.turn < self.max_turns and state.cost_usd < self.max_cost_usd:
                # 1. Render prompt + ask LLM.
                prompt = self._render_state_prompt(state)
                messages = [
                    {"role": "system", "content": ORCHESTRATOR_SYSTEM},
                    {"role": "user", "content": prompt},
                ]
                response = await self.llm.complete(
                    messages, response_format={"type": "json_object"},
                )

                # 2. Parse decision; on bad output, skip turn.
                decision = self._parse_decision(response.content)
                if decision is None:
                    logger.warning("orchestrator: bad LLM output on turn %d", state.turn)
                    state.turn += 1
                    continue

                # 3. Apply folding directive (in-place on multi_scale_summary).
                self._apply_folding(state, decision.folding_directive)

                # 4. Sufficiency-gated stop.
                if decision.worker == "stop":
                    if state.sufficiency.can_stop():
                        state.history.append(StepRecord(
                            turn=state.turn, worker="stop",
                            args_summary=_short(json.dumps(decision.args), 80),
                            result_summary=f"can_stop=True; stopping ({decision.reasoning})",
                            cost_usd=0.0, duration_ms=0,
                            folding_directive=decision.folding_directive,
                        ))
                        break
                    # Reject premature stop.
                    state.history.append(StepRecord(
                        turn=state.turn, worker="stop",
                        args_summary="(rejected)",
                        result_summary=(
                            f"stop rejected — sufficiency not met "
                            f"(domains={state.sufficiency.distinct_domains_visited}/3, "
                            f"offers={state.sufficiency.offer_count}/3, "
                            f"no_improvement={state.sufficiency.turns_since_offer_improvement}/5)"
                        ),
                        cost_usd=0.0, duration_ms=0,
                        folding_directive=decision.folding_directive,
                    ))
                    # A rejected stop still consumes a turn of "nothing
                    # happened" — bump no-progress counters so the
                    # sufficiency window doesn't stall indefinitely.
                    state.sufficiency.turns_since_offer_improvement += 1
                    state.consecutive_no_progress += 1
                    state.turn += 1
                    continue

                # 5. Dispatch worker; record StepRecord; mutate state.
                step = await self._dispatch_worker(state, session, decision)
                state.history.append(step)

                # 6. Validator replan handling.
                if decision.worker == "validator":
                    # Validator may have requested replan leads. Honor only if
                    # we haven't already replanned and the validator wasn't
                    # acceptable. (The validator worker pushes leads onto
                    # frontier directly during dispatch.)
                    if replans_used < self.max_replans:
                        # Did dispatch push leads? Check step's payload.
                        if step.result_summary.startswith("REPLAN"):
                            replans_used += 1

                # 7. Update sufficiency after the step.
                self._update_sufficiency(state, last_offer_count)
                if len(state.offers) > last_offer_count:
                    last_offer_count = len(state.offers)
                    state.sufficiency.turns_since_offer_improvement = 0
                    state.consecutive_no_progress = 0
                else:
                    state.consecutive_no_progress += 1
                    state.sufficiency.turns_since_offer_improvement += 1

                state.turn += 1

        return state

    # ---------------------------------------------------------------------
    # Prompt rendering
    # ---------------------------------------------------------------------

    def _render_state_prompt(self, state: OrchestratorState) -> str:
        # Goal anchor — verbatim spec at top (never paraphrased).
        spec_block = (
            "User's deal-hunting spec (DO NOT PARAPHRASE; this is the goal):\n"
            f"  {state.spec.model_dump_json(indent=2)}\n"
        )

        # Budget + sufficiency.
        suff = state.sufficiency
        suff_block = (
            f"\nTurn: {state.turn + 1}/{self.max_turns}  "
            f"Cost: ${state.cost_usd:.4f}/${self.max_cost_usd:.2f}\n"
            f"Sufficiency:\n"
            f"  distinct_domains_visited: {suff.distinct_domains_visited}/3 "
            f"{'✓' if suff.distinct_domains_visited >= 3 else '✗'}\n"
            f"  has_price_baseline: {suff.has_price_baseline}\n"
            f"  offer_count: {suff.offer_count}/3 "
            f"{'✓' if suff.offer_count >= 3 else '✗'}\n"
            f"  turns_since_offer_improvement: "
            f"{suff.turns_since_offer_improvement}/5 "
            f"{'✓' if suff.turns_since_offer_improvement >= 5 else '✗'}\n"
            f"  → can_stop: {suff.can_stop()}\n"
        )

        # Frontier.
        sorted_frontier = sorted(state.frontier, key=lambda t: -t.estimated_value)
        frontier_lines = [
            f"  [{t.id[:6]}] {t.intent!r} value={t.estimated_value:.2f} "
            f"depth={t.depth} url={t.current_url}"
            for t in sorted_frontier[:_FRONTIER_PROMPT_TOP_N]
        ]
        frontier_block = (
            f"\nFrontier ({len(state.frontier)} threads, top "
            f"{_FRONTIER_PROMPT_TOP_N} shown):\n"
            + ("\n".join(frontier_lines) if frontier_lines else "  (empty)")
            + "\n"
        )

        # Parked + current.
        current_block = ""
        if state.current_thread is not None:
            t = state.current_thread
            current_block = (
                f"\nCurrent thread: [{t.id[:6]}] {t.intent!r} "
                f"(findings={len(t.findings)}, visited={len(t.visited_urls)})\n"
            )

        parked_block = f"\nParked: {len(state.parked)} threads\n"

        # Offers.
        offers_block = f"\nOffers collected: {len(state.offers)}\n"
        if state.offers:
            offers_block += "\n".join(
                f"  - {o.title} @ ${o.price:.2f} ({o.retailer})"
                for o in state.offers[-3:]
            ) + "\n"

        # Multi-scale summary.
        mss = state.multi_scale_summary
        memory_block = "\nMulti-scale memory:\n"
        if mss.long_term:
            memory_block += "  Long-term (coarse):\n" + "\n".join(
                f"    [{b.turn_range[0]}-{b.turn_range[1]}] {b.summary}"
                for b in mss.long_term
            ) + "\n"
        if mss.recent:
            memory_block += "  Recent (fine-grained):\n" + "\n".join(
                f"    [{b.turn_range[0]}-{b.turn_range[1]}] {b.summary}"
                for b in mss.recent[-_RECENT_HISTORY_PROMPT_N:]
            ) + "\n"
        if mss.raw_latest:
            memory_block += f"  Latest observation:\n    {mss.raw_latest}\n"
        if not (mss.long_term or mss.recent or mss.raw_latest):
            memory_block += "  (empty — fresh run)\n"

        # Action memory summary (counts only).
        am_count = sum(len(v) for v in state.action_memory.values())
        am_block = (
            f"\nAction memory: {am_count} failed actions "
            f"across {len(state.action_memory)} URLs\n"
        )

        return (
            spec_block + suff_block + frontier_block + current_block
            + parked_block + offers_block + memory_block + am_block
            + "\nOutput the next decision as JSON."
        )

    # ---------------------------------------------------------------------
    # Decision parsing
    # ---------------------------------------------------------------------

    def _parse_decision(self, content: str) -> OrchestratorDecision | None:
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return None
        try:
            parsed = _DecisionJSON.model_validate(data)
        except ValidationError:
            return None
        return OrchestratorDecision(
            reasoning=parsed.reasoning,
            worker=parsed.worker,           # type: ignore[arg-type]
            args=parsed.args,
            folding_directive=parsed.folding_directive.model_dump(),
        )

    # ---------------------------------------------------------------------
    # Folding
    # ---------------------------------------------------------------------

    def _apply_folding(
        self, state: OrchestratorState, raw_directive: Any,
    ) -> None:
        """Mutate state.multi_scale_summary according to the LLM's directive.

        Defensive: the LLM-emitted directive may have bad target_steps or
        missing new_summary. Skip silently if so — folding is best-effort
        cosmetic compression, not load-bearing logic.
        """
        # The directive comes in as a dict embedded in _DecisionJSON; we
        # re-parse defensively.
        if not isinstance(raw_directive, dict):
            try:
                raw_directive = raw_directive.model_dump()
            except Exception:
                return
        d_type = raw_directive.get("type", "none")
        if d_type == "none":
            return
        new_summary = raw_directive.get("new_summary") or ""
        if not new_summary.strip():
            return
        target_steps = raw_directive.get("target_steps") or []

        # Compute turn_range from target_steps (clamp to current state).
        if target_steps:
            turn_range = (min(target_steps), max(target_steps))
        else:
            turn_range = (state.turn, state.turn)

        mss = state.multi_scale_summary

        if d_type == "granular_condense":
            mss.recent.append(FoldedBlock(
                summary=new_summary, turn_range=turn_range, scale="fine",
            ))
            # When recent exceeds threshold, the LLM should next emit a
            # deep_consolidate; we don't auto-collapse here (the LLM owns
            # the timing).
            return

        if d_type == "deep_consolidate":
            mss.long_term.append(FoldedBlock(
                summary=new_summary, turn_range=turn_range, scale="coarse",
            ))
            # The deep consolidate replaces older recent entries.
            mss.recent = mss.recent[-_DEEP_CONSOLIDATE_THRESHOLD:]
            return

    # ---------------------------------------------------------------------
    # Worker dispatch
    # ---------------------------------------------------------------------

    async def _dispatch_worker(
        self,
        state: OrchestratorState,
        session: BrowserSession,
        decision: OrchestratorDecision,
    ) -> StepRecord:
        start = time.monotonic()
        worker = decision.worker
        args_summary = _short(json.dumps(decision.args), 80)

        try:
            if worker == "search_planner":
                threads = await self.search_planner.plan(state.spec)
                state.frontier.extend(threads)
                result_summary = f"seeded {len(threads)} starting threads"

            elif worker == "page_reader":
                thread = self._pop_thread_for_dispatch(state, decision.args.get("thread_id"))
                if thread is None:
                    result_summary = "no thread to dispatch (frontier empty?)"
                else:
                    state.current_thread = thread
                    page_reader_result = await self.page_reader.explore(
                        thread, session, state, self.rate_limiter,
                    )
                    # Push spawned leads onto frontier with default est_value
                    for lead in page_reader_result.new_leads:
                        state.frontier.append(Thread(
                            id=str(uuid.uuid4()),
                            parent_id=thread.id,
                            intent=lead.intent,
                            current_url=lead.url,
                            depth=thread.depth + 1,
                            estimated_value=0.5,
                        ))
                    # Park the current thread back for potential future use
                    state.parked.append(thread)
                    state.current_thread = None
                    result_summary = (
                        f"PageReader returned: stop={page_reader_result.stop_reason} "
                        f"findings_added={len(page_reader_result.findings_added)} "
                        f"new_leads={len(page_reader_result.new_leads)}"
                    )

            elif worker == "lead_scorer":
                thread = self._find_thread(state, decision.args.get("thread_id"))
                if thread is None:
                    result_summary = "no thread to score"
                else:
                    score = await self.lead_scorer.score(thread, state)
                    thread.estimated_value = score
                    result_summary = f"scored thread {thread.id[:6]}: {score:.2f}"

            elif worker == "offer_extractor":
                thread = self._find_thread(state, decision.args.get("thread_id"))
                if thread is None:
                    result_summary = "no thread to extract from"
                else:
                    offers = await self.offer_extractor.extract(thread, state.spec)
                    state.offers.extend(offers)
                    result_summary = f"extracted {len(offers)} offer(s) from thread {thread.id[:6]}"

            elif worker == "validator":
                vd = await self.validator.validate(state.spec, state.offers)
                # Keep only the validator-approved offers.
                state.offers = list(vd.kept_offers)
                # Push suggested_leads as new frontier entries.
                for new_thread in vd.suggested_leads:
                    state.frontier.append(new_thread)
                marker = "REPLAN" if not vd.acceptable and vd.suggested_leads else "OK"
                result_summary = (
                    f"{marker} acceptable={vd.acceptable} "
                    f"kept={len(vd.kept_offers)} suggested_leads={len(vd.suggested_leads)}"
                )

            else:
                result_summary = f"unknown worker {worker!r}"

        except Exception as exc:
            logger.exception("orchestrator: worker %r raised", worker)
            result_summary = f"ERROR {type(exc).__name__}: {str(exc)[:120]}"

        duration_ms = int((time.monotonic() - start) * 1000)
        return StepRecord(
            turn=state.turn,
            worker=worker,
            args_summary=args_summary,
            result_summary=result_summary,
            cost_usd=0.0,                 # Phase 1 doesn't measure cost
            duration_ms=duration_ms,
            folding_directive=None,
            sub_trace=None,
        )

    def _pop_thread_for_dispatch(
        self, state: OrchestratorState, thread_id: str | None,
    ) -> Thread | None:
        """Pop the requested thread from frontier OR parked. If no ID, pop
        the highest-value frontier item."""
        if thread_id:
            for i, t in enumerate(state.frontier):
                if t.id.startswith(thread_id) or t.id == thread_id:
                    return state.frontier.pop(i)
            for i, t in enumerate(state.parked):
                if t.id.startswith(thread_id) or t.id == thread_id:
                    return state.parked.pop(i)
            return None
        if not state.frontier:
            return None
        # No ID → highest est_value.
        idx = max(range(len(state.frontier)),
                  key=lambda i: state.frontier[i].estimated_value)
        return state.frontier.pop(idx)

    def _find_thread(
        self, state: OrchestratorState, thread_id: str | None,
    ) -> Thread | None:
        """Find a thread by ID without removing it (for non-mutating workers).

        Fallbacks when no ID is supplied: prefer the most-recently-parked
        thread (typically the one PageReader just finished exploring),
        then the highest-value frontier item, then None.
        """
        if thread_id:
            for t in state.frontier:
                if t.id.startswith(thread_id) or t.id == thread_id:
                    return t
            for t in state.parked:
                if t.id.startswith(thread_id) or t.id == thread_id:
                    return t
            return None
        # No ID — pick the most natural default.
        if state.parked:
            return state.parked[-1]
        if state.frontier:
            return max(state.frontier, key=lambda t: t.estimated_value)
        return None

    # ---------------------------------------------------------------------
    # Sufficiency update
    # ---------------------------------------------------------------------

    def _update_sufficiency(
        self, state: OrchestratorState, prev_offer_count: int,
    ) -> None:
        """Recompute sufficiency from current state. Called every turn."""
        # Distinct domains across all visited URLs.
        domains: set[str] = set()
        all_threads = list(state.frontier) + list(state.parked)
        if state.current_thread is not None:
            all_threads.append(state.current_thread)
        for t in all_threads:
            for url in t.visited_urls:
                d = _domain(url)
                if d:
                    domains.add(d)
            if t.current_url:
                d = _domain(t.current_url)
                if d:
                    domains.add(d)

        state.sufficiency.distinct_domains_visited = len(domains)
        state.sufficiency.offer_count = len(state.offers)
        state.sufficiency.has_price_baseline = (
            len(state.offers) > 0
            or any(
                "$" in f.text
                for t in all_threads for f in t.findings
                if f.provenance == "observation"
            )
        )
        # turns_since_offer_improvement updated by caller


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."
