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
from dealbot.agents.tracing import NullTraceWriter, TraceWriter
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
# Threads with this many consecutive 0-finding PageReader dispatches are
# considered exhausted; the orchestrator skips them when picking dispatch
# targets. Prevents the "scroll an exhausted search page 19× in a row"
# pathology found in spike trace analysis. Set generously to allow recovery
# strategies after the first extraction (post-extraction agent needs a few
# dispatches to find unblacklisted listings).
_THREAD_EXHAUSTION_THRESHOLD = 6
# After this many failed offer_extractor calls on a thread, the forced-
# extraction guardrail stops re-firing on it. Prevents the infinite-retry
# loop when the extractor LLM repeatedly produces malformed JSON for a
# specific findings payload (spike found 18× WorkerOutputError in one run).
_MAX_EXTRACTION_FAILS = 3


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
        trace_writer: TraceWriter | None = None,
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
        self.trace_writer = trace_writer or NullTraceWriter()
        # Page reader also writes traces; propagate the writer.
        self.page_reader.trace_writer = self.trace_writer
        self.max_cost_usd = max_cost_usd
        self.max_replans = max_replans

    # ---------------------------------------------------------------------
    # Public entrypoint
    # ---------------------------------------------------------------------

    async def run(self, spec: WatchlistContext) -> OrchestratorState:
        state = OrchestratorState(spec=spec)
        replans_used = 0
        last_offer_count = 0
        search_planner_calls = 0

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

                # 2.5a. Cap search_planner. The orchestrator's prompt says
                # "use once at the start," but smaller LLMs ignore this and spam
                # it every other turn — generating duplicate threads and never
                # advancing past exploration. After the first call, any further
                # search_planner pick is rerouted to page_reader on the
                # highest-value available thread.
                if decision.worker == "search_planner":
                    if search_planner_calls >= 1 and (state.frontier or state.parked):
                        pool = list(state.frontier) + list(state.parked)
                        best = max(pool, key=lambda t: t.estimated_value)
                        logger.info(
                            "orchestrator: blocking duplicate search_planner; "
                            "rerouting to page_reader on %s", best.id[:6],
                        )
                        decision = OrchestratorDecision(
                            reasoning=(
                                f"BLOCKED: search_planner already called "
                                f"{search_planner_calls}×; rerouting to "
                                f"page_reader on {best.id[:6]}"
                            ),
                            worker="page_reader",
                            args={"thread_id": best.id},
                            folding_directive={},
                        )
                    else:
                        search_planner_calls += 1

                # 2.5b. Deterministic guardrail: if any thread has enough findings
                # to be harvested and offer_extractor hasn't run on it, force the
                # decision regardless of LLM choice. Without this, smaller models
                # tend to spam search_planner + page_reader forever and never
                # advance to extraction. (See spike trajectory: orchestrator
                # called search_planner 49× and offer_extractor 0×.)
                forced = self._maybe_force_offer_extractor(state)
                if forced is not None and decision.worker != "offer_extractor":
                    new_findings = len(forced.findings) - forced.findings_at_last_extraction
                    logger.info(
                        "orchestrator: forcing offer_extractor on thread %s "
                        "(findings=%d new=%d, LLM picked %s)",
                        forced.id[:6], len(forced.findings), new_findings,
                        decision.worker,
                    )
                    decision = OrchestratorDecision(
                        reasoning=(
                            f"FORCED: thread {forced.id[:6]} has "
                            f"{len(forced.findings)} findings; LLM had picked "
                            f"{decision.worker}"
                        ),
                        worker="offer_extractor",
                        args={"thread_id": forced.id},
                        folding_directive={},
                    )
                # 2.6. Observability: record the orchestrator's decision now,
                # before dispatch — so traces capture the LLM I/O even if
                # the subsequent dispatch crashes.
                try:
                    self.trace_writer.record_orchestrator_turn(
                        turn=state.turn,
                        prompt=messages,
                        response_content=response.content,
                        decision_summary=decision.reasoning,
                        worker_chosen=decision.worker,
                        forced=decision.reasoning.startswith(("FORCED:", "BLOCKED:")),
                    )
                except Exception as exc:
                    logger.warning("trace: orchestrator turn write failed: %s", exc)

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
                # Trace dispatch errors so the report shows them.
                if step.result_summary.startswith("ERROR"):
                    try:
                        self.trace_writer.record_error(
                            orchestrator_turn=state.turn,
                            worker=decision.worker,
                            error=step.result_summary,
                        )
                    except Exception:
                        pass
                # Record findings-count snapshot at successful extraction time.
                # Re-extraction guardrail fires when len(findings) grows by ≥3
                # past this watermark, so PageReader exploration after a
                # successful extract still translates into more offers.
                if decision.worker == "offer_extractor":
                    tid = decision.args.get("thread_id")
                    if isinstance(tid, str):
                        thread = self._find_thread(state, tid)
                        if thread is not None:
                            if step.result_summary.startswith("ERROR"):
                                # Failed extraction bumps the per-thread failure
                                # counter; once the cap is hit the guardrail
                                # stops re-firing on this thread (prevents the
                                # 18×-WorkerOutputError infinite loop).
                                thread.failed_extractions += 1
                            else:
                                thread.findings_at_last_extraction = len(thread.findings)

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

                # Hard stop when every thread is exhausted AND we have at
                # least one offer. Prevents the orchestrator from burning
                # remaining turns dispatching exhausted threads. If we have
                # zero offers, keep trying — exhausted threads may still be
                # all we have to work with.
                all_threads = list(state.frontier) + list(state.parked)
                if (
                    all_threads
                    and len(state.offers) > 0
                    and all(
                        t.consecutive_empty_dispatches >= _THREAD_EXHAUSTION_THRESHOLD
                        for t in all_threads
                    )
                ):
                    state.history.append(StepRecord(
                        turn=state.turn, worker="stop",
                        args_summary="(exhausted)",
                        result_summary=(
                            f"All {len(all_threads)} threads exhausted; "
                            f"stopping with {len(state.offers)} offers."
                        ),
                        cost_usd=0.0, duration_ms=0,
                        folding_directive=None,
                    ))
                    break

        # Flush trace report regardless of how the loop exits.
        try:
            self.trace_writer.finalize()
        except Exception:
            logger.warning("trace: finalize failed", exc_info=True)
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
        sub_trace: list[Any] | None = None

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
                        orchestrator_turn=state.turn,
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
                    # Bump exhaustion counter for diagnostic + scheduling.
                    if len(page_reader_result.findings_added) == 0:
                        thread.consecutive_empty_dispatches += 1
                    else:
                        thread.consecutive_empty_dispatches = 0
                    # Park the current thread back for potential future use
                    state.parked.append(thread)
                    state.current_thread = None
                    result_summary = (
                        f"PageReader returned: stop={page_reader_result.stop_reason} "
                        f"findings_added={len(page_reader_result.findings_added)} "
                        f"new_leads={len(page_reader_result.new_leads)}"
                    )
                    # Capture sub_trace so the trajectory shows what tools
                    # PageReader actually called per dispatch — critical for
                    # debugging why a dispatch returned 0 vs N findings.
                    sub_trace = list(page_reader_result.sub_trace)

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
                    # Pass already-extracted URLs so OfferExtractor skips them.
                    # Critical for re-extraction runs — without this, the LLM
                    # would re-extract the same listings from accumulated findings.
                    offers = await self.offer_extractor.extract(
                        thread, state.spec,
                        exclude_urls=list(thread.extracted_leaf_urls),
                    )
                    state.offers.extend(offers)
                    # Blacklist the URLs we just turned into offers so PageReader
                    # doesn't re-click them and so future extractions skip them.
                    for offer in offers:
                        if offer.url and offer.url not in thread.extracted_leaf_urls:
                            thread.extracted_leaf_urls.append(offer.url)
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
        # Capture the folding directive the LLM emitted so trajectory shows
        # whether multi-scale memory folding is being exercised (vs the LLM
        # always defaulting to "none"). Safe-parse since the LLM may emit
        # a malformed dict.
        folding_for_record: FoldingDirective | None = None
        if decision.folding_directive:
            try:
                folding_for_record = FoldingDirective.model_validate(
                    decision.folding_directive,
                )
            except Exception:
                folding_for_record = None
        return StepRecord(
            turn=state.turn,
            worker=worker,
            args_summary=args_summary,
            result_summary=result_summary,
            cost_usd=0.0,                 # Phase 1 doesn't measure cost
            duration_ms=duration_ms,
            folding_directive=folding_for_record,
            sub_trace=sub_trace,
        )

    def _pop_thread_for_dispatch(
        self, state: OrchestratorState, thread_id: str | None,
    ) -> Thread | None:
        """Pop the requested thread from frontier OR parked. If no ID, pop
        the highest-value frontier item. Skips exhausted threads (≥3
        consecutive empty PageReader dispatches) unless they're the only
        option AND we don't have a parked-but-non-exhausted alternative."""
        if thread_id:
            for i, t in enumerate(state.frontier):
                if t.id.startswith(thread_id) or t.id == thread_id:
                    return state.frontier.pop(i)
            for i, t in enumerate(state.parked):
                if t.id.startswith(thread_id) or t.id == thread_id:
                    return state.parked.pop(i)
            return None
        # No ID → pick highest-value NON-EXHAUSTED thread.
        candidates = [
            (i, t) for i, t in enumerate(state.frontier)
            if t.consecutive_empty_dispatches < _THREAD_EXHAUSTION_THRESHOLD
        ]
        if not candidates:
            # All frontier threads exhausted. Try parked next.
            for i, t in enumerate(state.parked):
                if t.consecutive_empty_dispatches < _THREAD_EXHAUSTION_THRESHOLD:
                    return state.parked.pop(i)
            return None
        idx = max(candidates, key=lambda pair: pair[1].estimated_value)[0]
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

    def _maybe_force_offer_extractor(
        self,
        state: OrchestratorState,
    ) -> Thread | None:
        """Find a thread with enough NEW findings since its last extraction.

        First-time extraction: fires when len(findings) >= 3. Re-extraction:
        fires when len(findings) - findings_at_last_extraction >= 3. This
        lets sustained PageReader exploration translate into additional
        extractions instead of accumulating wasted findings.

        Threads that have repeatedly failed extraction (>= _MAX_EXTRACTION_FAILS)
        are skipped to prevent infinite retry loops on malformed LLM output.

        Threshold matches the orchestrator prompt's guidance ("≥2 observation-
        grade findings"). We require 3 to give the page a chance to yield
        title + price + url.
        """
        threshold = 3
        all_threads = list(state.frontier) + list(state.parked)
        if state.current_thread is not None:
            all_threads.append(state.current_thread)
        rich = [
            t for t in all_threads
            if len(t.findings) - t.findings_at_last_extraction >= threshold
            and t.failed_extractions < _MAX_EXTRACTION_FAILS
        ]
        if not rich:
            return None
        # Prefer the thread with the most NEW findings to extract.
        return max(
            rich,
            key=lambda t: len(t.findings) - t.findings_at_last_extraction,
        )

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
