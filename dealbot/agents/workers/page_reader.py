"""PageReader subagent — the tool-using component.

When the orchestrator picks a thread to expand, it dispatches PageReader.
PageReader runs its own mini ReAct loop (max_turns=12) using the 9 browser
tools, then returns a summary + accumulated findings + spawned leads.

Loop invariants:
  - Ephemeral page snapshots: only the most recent snapshot is fed back to
    the LLM in the user prompt. Older snapshots leave a 1-line marker in
    history (no DOM noise accumulating).
  - Action memory injection: at dispatch start, failed actions on
    current_url are surfaced in the system prompt as "do not repeat these".
  - Classified retries: retriable errors get 1 retry. Permanent errors are
    written to action_memory immediately, never retried.
  - Scroll budget: max 3 scrolls per page. The 4th scroll is rejected.
  - Stopping: explicit `done` call, max_turns, OR 3 consecutive turns with
    no element_map change (loop detection).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from dealbot.agents.perception import PageSnapshot, snapshot_page
from dealbot.agents.prompts import PAGE_READER_SYSTEM, render_spec_summary
from dealbot.agents.state import (
    FailedAction,
    Finding,
    OrchestratorState,
    Thread,
    ToolCallRecord,
)
from dealbot.agents.tracing import NullTraceWriter, TraceWriter
from dealbot.agents.tools import (
    Action,
    ActionResult,
    BrowserTool,
    ClickAction,
    DomainRateLimiter,
    DoneAction,
    NavigateAction,
    ReadPageAction,
    RecordFindingAction,
    ScrollAction,
    SpawnLeadAction,
    TakeScreenshotAction,
    ToolContext,
    TypeAction,
)
from dealbot.llm.base import LLMClient
from dealbot.scrapers.browser_session import BrowserSession

logger = logging.getLogger(__name__)


_MAX_TURNS = 20
_MAX_RETRIES_PER_ACTION = 1
_MAX_SCROLLS_PER_PAGE = 5
_LOOP_DETECT_THRESHOLD = 3       # consecutive no-change snapshots → stop
_INITIAL_HYDRATION_GRACE_S = 1.5  # post-goto sleep before first snapshot
_SETTLEMENT_TIMEOUT_MS = 10_000   # SPAs need more than the 5s default


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class SpawnedLead:
    intent: str
    url: str


@dataclass
class PageReaderResult:
    findings_added: list[Finding]
    new_leads: list[SpawnedLead]
    sub_trace: list[ToolCallRecord]
    summary: str
    stop_reason: str               # "done" | "max_turns" | "loop" | "stuck"
    turns_used: int


# ---------------------------------------------------------------------------
# LLM response shape — the JSON we ask the model to emit each turn
# ---------------------------------------------------------------------------

class _ActionWrapper(BaseModel):
    thought: str
    action: dict[str, Any]
    model_config = {"extra": "forbid"}


# Map action.type → action Pydantic class
_ACTION_REGISTRY: dict[str, type[BaseModel]] = {
    "navigate": NavigateAction,
    "click": ClickAction,
    "type": TypeAction,
    "scroll": ScrollAction,
    "read_page": ReadPageAction,
    "record_finding": RecordFindingAction,
    "spawn_lead": SpawnLeadAction,
    "take_screenshot": TakeScreenshotAction,
    "done": DoneAction,
}


# ---------------------------------------------------------------------------
# PageReader
# ---------------------------------------------------------------------------

class PageReader:
    def __init__(
        self,
        llm: LLMClient,
        tools: list[BrowserTool],
        max_turns: int = _MAX_TURNS,
        trace_writer: TraceWriter | None = None,
    ) -> None:
        self.llm = llm
        self.tools: dict[str, BrowserTool] = {t.name: t for t in tools}
        self.max_turns = max_turns
        self.trace_writer: TraceWriter = trace_writer or NullTraceWriter()

    async def explore(
        self,
        thread: Thread,
        session: BrowserSession,
        state: OrchestratorState,
        rate_limiter: DomainRateLimiter,
        orchestrator_turn: int = 0,
    ) -> PageReaderResult:
        # Initial bookkeeping.
        initial_finding_count = len(thread.findings)
        sub_trace: list[ToolCallRecord] = []
        new_leads: list[SpawnedLead] = []
        scroll_count = 0
        recent_snapshot_keys: list[tuple] = []   # for loop detection

        # Inject action_memory for the current URL into the first turn.
        url_for_memory = thread.current_url or session.page.url
        failed_history = state.action_memory.get(url_for_memory, [])

        # Auto-navigate to the thread's target URL when the browser isn't
        # already there. Without this, the agent starts at about:blank and
        # would have to choose navigate() as its first action — a contract
        # too loose for smaller models. Keeps PageReader self-sufficient.
        if thread.current_url and session.page.url != thread.current_url:
            try:
                await rate_limiter.acquire(thread.current_url)
                # networkidle waits for fetches to settle — critical for SPAs
                # where domcontentloaded fires before hydration completes.
                await session.page.goto(
                    thread.current_url, wait_until="networkidle", timeout=30_000,
                )
                try:
                    await session.watchdog.wait_for_settlement(
                        after_action="goto",
                        timeout_ms=_SETTLEMENT_TIMEOUT_MS,
                        debounce_ms=500,
                    )
                except Exception:
                    pass
                # Pragmatic hydration grace — many SPAs paint content in a
                # micro-task after the network goes quiet.
                import asyncio
                await asyncio.sleep(_INITIAL_HYDRATION_GRACE_S)
                if not thread.visited_urls or thread.visited_urls[-1] != thread.current_url:
                    thread.visited_urls.append(thread.current_url)
            except Exception as exc:
                logger.warning(
                    "PageReader: initial goto(%r) failed: %s", thread.current_url, exc,
                )

        # Build LLM message history. System prompt + initial context only;
        # we'll append per-turn user messages + assistant replies as we go.
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": PAGE_READER_SYSTEM},
            {"role": "user", "content": self._render_initial_prompt(
                thread, state.spec, failed_history,
            )},
        ]

        for turn in range(self.max_turns):
            # 1. Take a snapshot of the current page.
            snap = await snapshot_page(session.page)
            snap_key = _snapshot_key(snap)

            # 1b. Visual screenshot for observability (best-effort).
            try:
                png = await session.page.screenshot(full_page=False, type="png")
                self.trace_writer.record_screenshot(
                    orchestrator_turn=orchestrator_turn,
                    sub_turn=turn,
                    label="snapshot",
                    png_bytes=png,
                )
            except Exception as exc:
                logger.debug("trace: screenshot failed: %s", exc)

            # Optional diagnostic: dump first snapshot per dispatch to disk
            # so we can see what the agent actually perceives on real pages.
            # Activated by SPIKE_SNAPSHOT_DIR env var.
            if turn == 0:
                import os
                diag_dir = os.environ.get("SPIKE_SNAPSHOT_DIR")
                if diag_dir:
                    try:
                        from pathlib import Path
                        import time as _time
                        Path(diag_dir).mkdir(parents=True, exist_ok=True)
                        stamp = int(_time.time() * 1000)
                        fname = Path(diag_dir) / f"snap_{stamp}_elems{len(snap.element_map)}.txt"
                        fname.write_text(
                            f"URL: {snap.url}\n"
                            f"TITLE: {snap.title}\n"
                            f"CHARS: {snap.char_count}\n"
                            f"ELEMENT_MAP_SIZE: {len(snap.element_map)}\n"
                            f"DETECTED_MODALS: {snap.detected_modals}\n"
                            f"REDACTIONS: {snap.redactions}\n"
                            f"---TEXT---\n{snap.text[:8000]}"
                        )
                    except Exception as exc:
                        logger.warning("snapshot diag dump failed: %s", exc)

            # 2. Loop detection — same key N consecutive times → stop.
            if recent_snapshot_keys and recent_snapshot_keys[-1] == snap_key:
                recent_snapshot_keys.append(snap_key)
                if len(recent_snapshot_keys) >= _LOOP_DETECT_THRESHOLD:
                    return PageReaderResult(
                        findings_added=thread.findings[initial_finding_count:],
                        new_leads=new_leads,
                        sub_trace=sub_trace,
                        summary=(
                            f"Stopped at turn {turn}: {_LOOP_DETECT_THRESHOLD} "
                            f"consecutive identical snapshots."
                        ),
                        stop_reason="loop",
                        turns_used=turn,
                    )
            else:
                recent_snapshot_keys = [snap_key]

            # 3. Build per-turn user prompt with the current snapshot (ephemeral —
            # not retained in next turn's prompt).
            turn_user = self._render_turn_prompt(
                snap=snap,
                turn=turn,
                scroll_count=scroll_count,
                findings_count=len(thread.findings),
                extracted_leaf_urls=thread.extracted_leaf_urls,
            )
            messages.append({"role": "user", "content": turn_user})

            # Capture the prompt that's about to be sent (snapshot the list
            # because we'll mutate it afterwards).
            prompt_snapshot = [dict(m) for m in messages]

            # 4. LLM call asking for an Action JSON.
            response = await self.llm.complete(
                messages, response_format={"type": "json_object"},
            )
            messages.append({"role": "assistant", "content": response.content})

            # Trim ephemeral snapshot user msgs from history: keep last 2 only.
            messages = _trim_ephemeral_history(messages)

            # 5. Parse the LLM's action.
            action, parse_error = self._parse_action(response.content)
            if parse_error:
                messages.append({"role": "user", "content": (
                    f"Your action JSON was invalid: {parse_error}. "
                    "Re-emit your action."
                )})
                continue

            # 6. Pre-dispatch policy enforcement.
            if isinstance(action, ScrollAction):
                scroll_count += 1
                if scroll_count > _MAX_SCROLLS_PER_PAGE:
                    messages.append({"role": "user", "content": (
                        f"Scroll budget exhausted ({_MAX_SCROLLS_PER_PAGE}/3). "
                        "Pick a different action."
                    )})
                    continue

            # 7. Build ToolContext + dispatch with retry on retriable errors.
            ctx = ToolContext(
                page=session.page,
                session=session,
                state=state,
                current_thread=thread,
                rate_limiter=rate_limiter,
                turn=state.turn,
            )
            tool = self.tools.get(action.type)
            if tool is None:
                messages.append({"role": "user", "content": (
                    f"Unknown tool {action.type!r}."
                )})
                continue

            result = await self._dispatch_with_retry(tool, action, ctx)

            # Observability hook: record this PageReader sub-turn with full
            # LLM I/O + the snapshot text the agent saw.
            try:
                self.trace_writer.record_page_reader_turn(
                    orchestrator_turn=orchestrator_turn,
                    sub_turn=turn,
                    url=snap.url,
                    snapshot_text=snap.text,
                    element_map_size=len(snap.element_map),
                    prompt=prompt_snapshot,
                    response_content=response.content,
                    action_summary=f"{tool.name}({_short(action.model_dump_json(), 100)})",
                    result_summary=(
                        f"success={result.success}"
                        + (f" error={result.error.error_type}" if result.error else "")
                    ),
                )
            except Exception as exc:
                logger.debug("trace: page_reader turn record failed: %s", exc)

            # 8. Side effects: record sub-trace, handle done/spawn/finding/error.
            sub_trace.append(_to_record(tool.name, action, result))

            if not result.success and result.error and not result.error.retriable:
                # Permanent failure → write to action_memory.
                state.action_memory.setdefault(url_for_memory, []).append(
                    FailedAction(
                        tool=tool.name,
                        args_summary=_short(action.model_dump_json(), 80),
                        error_type=result.error.error_type,
                        turn=state.turn,
                    )
                )

            if isinstance(action, DoneAction):
                return PageReaderResult(
                    findings_added=thread.findings[initial_finding_count:],
                    new_leads=new_leads,
                    sub_trace=sub_trace,
                    summary=action.reason,
                    stop_reason="done",
                    turns_used=turn + 1,
                )

            if isinstance(action, SpawnLeadAction) and result.success:
                new_leads.append(SpawnedLead(intent=action.intent, url=action.url))

            # 9. Append a compact tool-result message for the LLM's next turn.
            messages.append({"role": "user", "content": _summarize_result_for_llm(
                tool.name, result,
            )})

        # Out of turns.
        return PageReaderResult(
            findings_added=thread.findings[initial_finding_count:],
            new_leads=new_leads,
            sub_trace=sub_trace,
            summary=f"Stopped: max_turns ({self.max_turns}) reached.",
            stop_reason="max_turns",
            turns_used=self.max_turns,
        )

    # ---------------------------------------------------------------------
    # Internals
    # ---------------------------------------------------------------------

    def _render_initial_prompt(
        self,
        thread: Thread,
        spec: Any,
        failed_history: list[FailedAction],
    ) -> str:
        memory_block = ""
        if failed_history:
            entries = "\n".join(
                f"- {f.tool}({f.args_summary}) → {f.error_type}"
                for f in failed_history[-5:]
            )
            memory_block = (
                "\n\nThe following actions failed on this URL previously and "
                "should NOT be repeated:\n" + entries
            )
        prior_findings = ""
        if thread.findings:
            entries = "\n".join(
                f"- [{f.provenance}] {f.text}" for f in thread.findings[-5:]
            )
            prior_findings = (
                "\n\nFindings already recorded on this thread:\n" + entries
            )
        return (
            f"User's spec: {render_spec_summary(spec)}\n"
            f"Your thread's intent: {thread.intent!r}\n"
            f"Current URL: {thread.current_url or '(none — call navigate first)'}"
            f"{prior_findings}{memory_block}\n\n"
            "Begin exploration. Emit one action per turn as JSON."
        )

    def _render_turn_prompt(
        self,
        snap: PageSnapshot,
        turn: int,
        scroll_count: int,
        findings_count: int,
        extracted_leaf_urls: list[str] | None = None,
    ) -> str:
        scroll_left = max(0, _MAX_SCROLLS_PER_PAGE - scroll_count)
        turn_left = self.max_turns - turn
        # Full page text — no truncation. The 4000-char cap was hiding real
        # listings (spike found they start at ~7k chars after modal/privacy
        # banner noise). Cost is fine at this scale.
        page_text = snap.text
        extracted_block = ""
        if extracted_leaf_urls:
            recent = extracted_leaf_urls[-10:]
            extracted_block = (
                "\nAlready extracted from these URLs — do NOT click into them again, "
                "find DIFFERENT listings on this page:\n"
                + "\n".join(f"  - {u}" for u in recent)
                + "\n"
            )
        captcha_block = ""
        if snap.captcha_detected:
            captcha_block = (
                "\n⚠ CAPTCHA / bot-challenge detected on this page. Do NOT try "
                "to solve it. Either spawn_lead to a different domain or call "
                "done() with reason='captcha_blocked'.\n"
            )
        return (
            f"Turn {turn + 1}/{self.max_turns} (remaining: {turn_left}).\n"
            f"Scroll budget left: {scroll_left}/3.\n"
            f"Findings recorded this dispatch: {findings_count}.\n"
            f"Current URL: {snap.url}\n"
            f"{captcha_block}"
            f"{extracted_block}\n"
            f"Page state (CDP perception):\n{page_text}\n\n"
            "Emit the next action as JSON."
        )

    def _parse_action(self, content: str) -> tuple[Action | None, str | None]:
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            return None, f"invalid JSON: {exc}"
        try:
            wrapper = _ActionWrapper.model_validate(data)
        except ValidationError as exc:
            return None, f"missing thought/action wrapper: {exc.errors()[:1]}"
        action_type = wrapper.action.get("type") if isinstance(wrapper.action, dict) else None
        if not action_type:
            return None, "action.type missing"
        schema = _ACTION_REGISTRY.get(action_type)
        if schema is None:
            return None, f"unknown action.type {action_type!r}"
        try:
            action = schema.model_validate(wrapper.action)
        except ValidationError as exc:
            return None, f"action validation failed: {exc.errors()[:2]}"
        return action, None  # type: ignore[return-value]

    async def _dispatch_with_retry(
        self, tool: BrowserTool, action: Action, ctx: ToolContext,
    ) -> ActionResult:
        result = await tool.execute(action, ctx)
        if result.success or result.error is None:
            return result
        if not result.error.retriable:
            return result
        for _ in range(_MAX_RETRIES_PER_ACTION):
            retry = await tool.execute(action, ctx)
            if retry.success:
                return retry
            result = retry
            if result.error is None or not result.error.retriable:
                break
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snapshot_key(snap: PageSnapshot) -> tuple:
    """Loop-detection key: URL + top-50 sorted element_id hash."""
    ids = sorted(snap.element_map.keys())[:50]
    return (snap.url, tuple(ids))


def _trim_ephemeral_history(
    messages: list[dict[str, Any]], keep_last_n: int = 2,
) -> list[dict[str, Any]]:
    """Keep the system + initial user msg + most recent N user/assistant pairs.

    Older user messages (which contain page snapshots) get dropped — they're
    pure noise once the action they prompted has been taken. The assistant
    replies stay so the LLM can see its own reasoning chain.
    """
    if len(messages) <= 2:
        return messages
    head = messages[:2]                # system + initial user
    tail = messages[2:]                # the back-and-forth

    # Identify user-snapshot messages — those that start with "Turn N/"
    keep_indices: list[int] = []
    snapshot_indices = [
        i for i, m in enumerate(tail)
        if m["role"] == "user" and m["content"].startswith("Turn ")
    ]
    # Keep only the last `keep_last_n` of these; drop earlier ones.
    drop = set(snapshot_indices[:-keep_last_n]) if len(snapshot_indices) > keep_last_n else set()
    trimmed_tail = [m for i, m in enumerate(tail) if i not in drop]
    return head + trimmed_tail


def _to_record(tool_name: str, action: Action, result: ActionResult) -> ToolCallRecord:
    return ToolCallRecord(
        tool=tool_name,
        args_summary=_short(action.model_dump_json(), 100),
        result_summary=_summarize_result_for_record(result),
        duration_ms=0,  # filled later by the orchestrator with real timing
        error=None if result.error is None else result.error.message[:120],
    )


def _summarize_result_for_record(result: ActionResult) -> str:
    if not result.success and result.error:
        return f"FAIL {result.error.error_type}: {result.error.message[:80]}"
    if result.change_summary:
        cs = result.change_summary
        return (
            f"OK page_changed={cs.page_changed} "
            f"({cs.elements_before}→{cs.elements_after} elements, "
            f"+{len(cs.new_element_ids)}/-{len(cs.gone_element_ids)})"
        )
    return "OK"


def _summarize_result_for_llm(tool_name: str, result: ActionResult) -> str:
    """Compact textual feedback for the LLM's next turn."""
    if not result.success and result.error:
        return (
            f"{tool_name} → ERROR ({result.error.error_type}, "
            f"retriable={result.error.retriable}): {result.error.message[:200]}"
        )
    if result.change_summary:
        cs = result.change_summary
        diff_marker = "PAGE_CHANGED" if cs.page_changed else "NO_VISIBLE_CHANGE"
        return (
            f"{tool_name} → OK {diff_marker} ({cs.elements_before}→{cs.elements_after} elems)"
        )
    if result.payload:
        return f"{tool_name} → OK {_short(json.dumps(result.payload), 120)}"
    return f"{tool_name} → OK"


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."
