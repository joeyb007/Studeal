"""CDP-direct page perception for the browser agent.

Produces a compact, LLM-readable snapshot of a Playwright Page by fusing two CDP
sources:

  - `Accessibility.getFullAXTree` — semantic role, name, value, parent/child
  - `DOMSnapshot.captureSnapshot` — bbox, tag, attrs, computed cursor style

Each element gets keyed on its CDP `backend_node_id` (stable across snapshots,
maps directly to CDP for action dispatch). The serialized output uses
browser-use's indented pseudo-tree format:

    [42]<button aria-label="Search" /> "Search"
        <span /> "Submit"
    [43]<a href="..." /> "Sign in"

`[id]` brackets denote interactive elements (button, link, input, role=button,
cursor:pointer, etc.) — the LLM is told to reference them when calling
click/type tools. Non-interactive containers appear unbracketed.

Architectural reference: browser-use's `browser_use/dom/service.py` +
`browser_use/dom/serializer/serializer.py`. We adopt the same shape but with a
narrower v1 scope — no shadow DOM piercing, no JS event-listener detection via
`DOMDebugger.getEventListeners`, no 99% bounding-box containment collapse, no
new-element `*` markers. Those land in 1.2b if integration tests show we need
them on real retailer pages.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Page

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Whitelists / constants
# ---------------------------------------------------------------------------

# HTML attributes kept in the serialized tree — chosen for signal-per-token.
# Skips style/data-*/event-handler attrs to stay under the ~4k token budget.
_ATTR_WHITELIST: frozenset[str] = frozenset({
    "id", "name", "type", "role", "placeholder", "value",
    "aria-label", "aria-expanded", "aria-checked", "aria-selected",
    "alt", "required", "checked", "selected", "disabled", "readonly",
    "href", "title", "for",
})

# ARIA roles that are always interactive regardless of tag.
_INTERACTIVE_ROLES: frozenset[str] = frozenset({
    "button", "link", "textbox", "combobox", "listbox", "menuitem",
    "menuitemcheckbox", "menuitemradio", "tab", "checkbox", "radio",
    "switch", "slider", "spinbutton", "searchbox", "option", "treeitem",
})

# HTML tags that are always interactive regardless of role.
_INTERACTIVE_TAGS: frozenset[str] = frozenset({
    "button", "a", "input", "select", "textarea",
})

# Tags whose subtree we skip entirely (never serialized).
_SKIP_TAGS: frozenset[str] = frozenset({
    "script", "style", "noscript", "svg", "path", "head", "meta", "link", "title",
})

# Truncation limits for serialized text — guards against runaway page content.
_NAME_MAX = 200
_ATTR_VALUE_MAX = 500     # need full URLs in href so LLM records real links
_TEXT_NODE_MAX = 200

# Containment threshold for the 0.99 collapse — a child whose bbox sits ≥99%
# inside an interactive parent's bbox is hidden from the LLM. Inputs / selects /
# textareas are exempt (always interactive on their own).
_CONTAINMENT_THRESHOLD = 0.99
_CONTAINMENT_EXEMPT_TAGS: frozenset[str] = frozenset({"input", "select", "textarea"})

# How many "candidate" nodes (not already interactive, but plausibly clickable)
# we'll probe via CDP DOMDebugger.getEventListeners per snapshot. Each call is
# a CDP round trip, so capped for latency.
_LISTENER_PROBE_CAP = 30

# Tags eligible for listener probing — divs and spans with JS click handlers
# are the canonical missed-interactive case on retailer pages.
_LISTENER_PROBE_TAGS: frozenset[str] = frozenset({"div", "span", "li", "section", "article"})

# Modal detection — AX tree roles + ARIA properties that mean "dialog".
_MODAL_ROLES: frozenset[str] = frozenset({"dialog", "alertdialog"})

# Prompt-injection patterns. Anything matching gets replaced with `[REDACTED]`
# in serialized text. Conservative: we want false-positives over false-negatives
# because the LLM should *never* treat page content as instructions.
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore\s+(all\s+)?(prior|previous|earlier|above)\s+instructions", re.IGNORECASE),
    re.compile(r"\bsystem\s*:", re.IGNORECASE),
    re.compile(r"you\s+are\s+(now|actually)\s+", re.IGNORECASE),
    re.compile(r"forget\s+everything", re.IGNORECASE),
    re.compile(r"new\s+instructions\s*:", re.IGNORECASE),
    re.compile(r"<!--.*?-->", re.DOTALL),  # HTML comments — sneaky injection vector
    re.compile(r"<\s*script\b.*?<\s*/\s*script\s*>", re.IGNORECASE | re.DOTALL),
)


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------

@dataclass
class ElementRef:
    """One element in the page, keyed for action dispatch via backend_node_id."""

    backend_node_id: int
    role: str
    name: str
    tag_name: str | None = None
    value: str | None = None
    bbox: tuple[float, float, float, float] | None = None  # (x, y, w, h)
    is_interactive: bool = False
    is_modal: bool = False                 # flagged by modal detection
    has_js_listener: bool = False          # discovered via DOMDebugger.getEventListeners
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class PageSnapshot:
    """The thing PageReader hands to the LLM each turn."""

    text: str                              # indented tree, LLM-facing
    element_map: dict[int, ElementRef]     # backend_node_id → ElementRef
    url: str
    title: str
    char_count: int                        # for context-budget monitoring
    detected_modals: list[int] = field(default_factory=list)  # backend_node_ids
    redactions: int = 0                    # count of prompt-injection redactions applied


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

async def snapshot_page(page: Page) -> PageSnapshot:
    """Capture a CDP-based perception snapshot of `page`.

    Fetches AXTree + DOMSnapshot in parallel, fuses them on backend_node_id,
    filters to visible+useful nodes, serializes as an indented pseudo-tree.

    Returns a PageSnapshot ready to hand to an LLM (`.text`) with an
    element_map for action dispatch (`.element_map[backend_node_id]`).
    """
    cdp = await page.context.new_cdp_session(page)
    try:
        # 1. Parallel CDP fetch of AX tree + DOMSnapshot.
        ax_data, dom_data = await asyncio.gather(
            cdp.send("Accessibility.getFullAXTree", {}),
            cdp.send("DOMSnapshot.captureSnapshot", {
                "computedStyles": ["cursor"],
                "includeDOMRects": True,
            }),
        )

        ax_nodes: list[dict[str, Any]] = ax_data.get("nodes", [])
        bbox_by_id, tag_by_id, attrs_by_id, cursor_by_id = _index_dom_snapshot(dom_data)

        # 2. Build element_map from AX tree, enriching with DOMSnapshot data.
        element_map: dict[int, ElementRef] = {}
        ax_id_to_backend: dict[str, int] = {}

        for node in ax_nodes:
            backend_id = node.get("backendDOMNodeId")
            if backend_id is None:
                continue
            ax_node_id = node.get("nodeId")
            if ax_node_id:
                ax_id_to_backend[ax_node_id] = backend_id

        for node in ax_nodes:
            backend_id = node.get("backendDOMNodeId")
            if backend_id is None or node.get("ignored"):
                continue
            tag = tag_by_id.get(backend_id)
            if tag in _SKIP_TAGS:
                continue
            role = _get_value(node.get("role"))
            name = _get_value(node.get("name")) or ""
            value = _get_value(node.get("value"))
            bbox = bbox_by_id.get(backend_id)
            attrs = attrs_by_id.get(backend_id, {})
            cursor = cursor_by_id.get(backend_id, "")
            if bbox is None or bbox[2] <= 0 or bbox[3] <= 0:
                continue
            is_interactive = _is_interactive(role, tag, attrs, cursor)
            element_map[backend_id] = ElementRef(
                backend_node_id=backend_id,
                role=role or tag or "generic",
                name=name,
                tag_name=tag,
                value=str(value) if value is not None else None,
                bbox=bbox,
                is_interactive=is_interactive,
                attributes={k: v for k, v in attrs.items() if k in _ATTR_WHITELIST},
            )

        # 3. Phase 1.2b: probe JS event listeners on candidate non-interactive
        # divs/spans. Stays inside the CDP try-block since it makes more calls.
        await _probe_js_listeners(cdp, element_map)
    finally:
        try:
            await cdp.detach()
        except Exception:
            pass

    # 4. Phase 1.2b: detect modals (AX role + aria-modal attribute).
    detected_modals = _detect_modals(element_map, ax_nodes)

    # 5. Build parent → children map from AX parentId pointers (used by both
    # containment collapse and serialization).
    children_of = _build_children_map(element_map, ax_nodes, ax_id_to_backend)

    # 6. Phase 1.2b: collapse children fully contained within interactive parents.
    _collapse_contained(element_map, children_of)
    # Containment may have orphaned entries in children_of (children of a
    # surviving node may have been dropped). Rebuild for serialization.
    children_of = _build_children_map(element_map, ax_nodes, ax_id_to_backend)

    # 7. Serialize, scrubbing prompt-injection patterns from text.
    text, redactions = _serialize_tree(element_map, children_of)

    url = page.url
    try:
        title = await page.title()
    except Exception:
        title = ""

    return PageSnapshot(
        text=text,
        element_map=element_map,
        url=url,
        title=title,
        char_count=len(text),
        detected_modals=detected_modals,
        redactions=redactions,
    )


# ---------------------------------------------------------------------------
# DOMSnapshot indexing
# ---------------------------------------------------------------------------

def _index_dom_snapshot(dom_data: dict[str, Any]) -> tuple[
    dict[int, tuple[float, float, float, float]],
    dict[int, str],
    dict[int, dict[str, str]],
    dict[int, str],
]:
    """Extract per-backend_node_id bbox, tag, attributes, cursor style.

    DOMSnapshot is column-oriented with a shared string table. For each
    document, walk parallel arrays in `nodes` to build per-node mappings,
    then walk `layout.nodeIndex`/`layout.bounds` to attach bboxes.
    """
    strings: list[str] = dom_data.get("strings", [])
    docs: list[dict[str, Any]] = dom_data.get("documents", [])

    bbox_by: dict[int, tuple[float, float, float, float]] = {}
    tag_by: dict[int, str] = {}
    attrs_by: dict[int, dict[str, str]] = {}
    cursor_by: dict[int, str] = {}

    def _safe_str(idx: int) -> str:
        return strings[idx] if 0 <= idx < len(strings) else ""

    for doc in docs:
        nodes = doc.get("nodes", {})
        layout = doc.get("layout", {})

        backend_ids: list[int] = nodes.get("backendNodeId", [])
        node_names: list[int] = nodes.get("nodeName", [])
        attributes: list[list[int]] = nodes.get("attributes", [])

        # Tag name (lower-cased), per node index.
        for i, backend_id in enumerate(backend_ids):
            if i < len(node_names):
                tag_by[backend_id] = _safe_str(node_names[i]).lower()

        # Attributes — array of [key_idx, val_idx, key_idx, val_idx, ...] per node.
        for i, backend_id in enumerate(backend_ids):
            if i < len(attributes) and attributes[i]:
                pairs = attributes[i]
                node_attrs: dict[str, str] = {}
                for j in range(0, len(pairs) - 1, 2):
                    key = _safe_str(pairs[j]).lower()
                    val = _safe_str(pairs[j + 1])
                    if key:
                        node_attrs[key] = val
                if node_attrs:
                    attrs_by[backend_id] = node_attrs

        # Layout: parallel arrays. `nodeIndex[i]` says which node row index
        # entry i belongs to; `bounds[i]` is its [x, y, w, h]; `styles[i]`
        # carries the requested computedStyles (cursor) by string-table index.
        layout_node_indices: list[int] = layout.get("nodeIndex", [])
        bounds: list[list[float]] = layout.get("bounds", [])
        styles: list[list[int]] = layout.get("styles", [])

        for i, node_idx in enumerate(layout_node_indices):
            if not (0 <= node_idx < len(backend_ids)):
                continue
            backend_id = backend_ids[node_idx]
            if i < len(bounds):
                b = bounds[i]
                if len(b) >= 4:
                    bbox_by[backend_id] = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
            if i < len(styles) and styles[i]:
                # First (and only) requested style is cursor.
                cursor_idx = styles[i][0]
                if cursor_idx >= 0:
                    cursor_by[backend_id] = _safe_str(cursor_idx)

    return bbox_by, tag_by, attrs_by, cursor_by


# ---------------------------------------------------------------------------
# Interactivity heuristic
# ---------------------------------------------------------------------------

def _is_interactive(
    role: str | None, tag: str | None, attrs: dict[str, str], cursor: str,
) -> bool:
    """Combine signals: ARIA role, native tag, onclick attr, cursor style."""
    if role and role in _INTERACTIVE_ROLES:
        return True
    if tag and tag in _INTERACTIVE_TAGS:
        return True
    if "onclick" in attrs:
        return True
    if cursor == "pointer":
        return True
    if attrs.get("contenteditable") in ("", "true"):
        return True
    return False


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _build_children_map(
    element_map: dict[int, ElementRef],
    ax_nodes: list[dict[str, Any]],
    ax_id_to_backend: dict[str, int],
) -> dict[int, list[int]]:
    """Parent → list of children, keyed on backend_node_id.

    Derived from AX parentId pointers, translated through ax_id_to_backend.
    Only includes nodes that survived to element_map (so containment-collapsed
    children are naturally absent).
    """
    children_of: dict[int, list[int]] = {}
    for node in ax_nodes:
        backend_id = node.get("backendDOMNodeId")
        if backend_id is None or backend_id not in element_map:
            continue
        parent_ax_id = node.get("parentId")
        if not parent_ax_id:
            continue
        parent_backend = ax_id_to_backend.get(parent_ax_id)
        if parent_backend is not None and parent_backend in element_map:
            children_of.setdefault(parent_backend, []).append(backend_id)
    return children_of


def _serialize_tree(
    element_map: dict[int, ElementRef],
    children_of: dict[int, list[int]],
) -> tuple[str, int]:
    """Indented pseudo-tree, browser-use convention. Returns (text, redactions).

    Format per node:
      Interactive:    [123]<tag attr="v" /> "name"
      Modal:          [MODAL] [123]<tag /> "name"
      Non-interactive: <tag /> "name"

    Nodes with no name AND no interactive descendants are pruned (noise).
    All visible text passes through _scrub_text — the LLM never sees raw
    page content that might contain prompt-injection patterns.
    """
    # Identify roots (entries in element_map with no parent in element_map).
    parent_of: dict[int, int] = {}
    for parent, kids in children_of.items():
        for k in kids:
            parent_of[k] = parent
    roots = [bid for bid in element_map if bid not in parent_of]

    # Walk the tree and decide who survives the noise prune.
    keep: set[int] = set()

    def _mark_keep(bid: int) -> bool:
        elem = element_map[bid]
        keep_self = elem.is_interactive or bool(elem.name)
        kept_any_child = False
        for child in children_of.get(bid, []):
            if _mark_keep(child):
                kept_any_child = True
        if keep_self or kept_any_child:
            keep.add(bid)
            return True
        return False

    for root in roots:
        _mark_keep(root)

    # Emit.
    lines: list[str] = []
    redactions = 0

    def _emit(bid: int, depth: int) -> None:
        nonlocal redactions
        if bid not in keep:
            return
        elem = element_map[bid]
        indent = "\t" * depth
        tag = elem.tag_name or elem.role or "div"
        modal_prefix = "[MODAL] " if elem.is_modal else ""

        if elem.is_interactive:
            attrs_str = _format_attrs(elem.attributes)
            line = f"{indent}{modal_prefix}[{elem.backend_node_id}]<{tag}{attrs_str} />"
        else:
            line = f"{indent}{modal_prefix}<{tag} />"

        if elem.name:
            scrubbed, count = _scrub_text(elem.name)
            redactions += count
            name_safe = _truncate(scrubbed, _NAME_MAX)
            line += f' "{name_safe}"'

        lines.append(line)

        for child in children_of.get(bid, []):
            _emit(child, depth + 1)

    for root in roots:
        _emit(root, 0)

    return "\n".join(lines), redactions


def _format_attrs(attrs: dict[str, str]) -> str:
    if not attrs:
        return ""
    parts: list[str] = []
    for k, v in attrs.items():
        v_safe = _truncate(v, _ATTR_VALUE_MAX).replace('"', "&quot;")
        parts.append(f'{k}="{v_safe}"')
    return " " + " ".join(parts)


def _truncate(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def _get_value(field_obj: Any) -> Any:
    """AX node fields are {type, value, ...} dicts; extract the value."""
    if isinstance(field_obj, dict):
        return field_obj.get("value")
    return field_obj


# ---------------------------------------------------------------------------
# Phase 1.2b: prompt-injection text filter
# ---------------------------------------------------------------------------

def _scrub_text(text: str) -> tuple[str, int]:
    """Replace prompt-injection patterns with [REDACTED]. Returns (text, count)."""
    if not text:
        return text, 0
    count = 0
    cleaned = text
    for pat in _INJECTION_PATTERNS:
        cleaned, n = pat.subn("[REDACTED]", cleaned)
        count += n
    return cleaned, count


# ---------------------------------------------------------------------------
# Phase 1.2b: 0.99 bounding-box containment collapse
# ---------------------------------------------------------------------------

def _bbox_containment(parent: tuple[float, float, float, float],
                      child: tuple[float, float, float, float]) -> float:
    """Fraction of child's area that overlaps with parent. 0.0-1.0."""
    px, py, pw, ph = parent
    cx, cy, cw, ch = child
    if cw <= 0 or ch <= 0:
        return 0.0
    ix1 = max(px, cx)
    iy1 = max(py, cy)
    ix2 = min(px + pw, cx + cw)
    iy2 = min(py + ph, cy + ch)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    overlap = (ix2 - ix1) * (iy2 - iy1)
    return overlap / (cw * ch)


def _collapse_contained(
    element_map: dict[int, ElementRef],
    children_of: dict[int, list[int]],
) -> None:
    """Drop children whose bbox sits ≥99% inside an interactive parent's bbox.

    Mutates `element_map` in place. Form inputs / selects / textareas are
    exempt — they're always interactive on their own and must remain
    individually addressable even when nested inside a clickable card.
    """
    to_drop: set[int] = set()
    for parent_id, child_ids in children_of.items():
        parent = element_map.get(parent_id)
        if parent is None or not parent.is_interactive or parent.bbox is None:
            continue
        for cid in child_ids:
            child = element_map.get(cid)
            if child is None or child.bbox is None:
                continue
            if child.tag_name in _CONTAINMENT_EXEMPT_TAGS:
                continue
            # Recurse into descendants — collapse the whole subtree.
            stack = [cid]
            while stack:
                node_id = stack.pop()
                node = element_map.get(node_id)
                if node is None or node.bbox is None:
                    continue
                if node.tag_name in _CONTAINMENT_EXEMPT_TAGS:
                    continue
                if _bbox_containment(parent.bbox, node.bbox) >= _CONTAINMENT_THRESHOLD:
                    to_drop.add(node_id)
                    stack.extend(children_of.get(node_id, []))

    for nid in to_drop:
        element_map.pop(nid, None)


# ---------------------------------------------------------------------------
# Phase 1.2b: modal detection
# ---------------------------------------------------------------------------

def _detect_modals(
    element_map: dict[int, ElementRef],
    ax_nodes: list[dict[str, Any]],
) -> list[int]:
    """Return backend_node_ids of elements that look like blocking modals.

    Heuristics (any one is sufficient):
      - AX role is "dialog" or "alertdialog"
      - element has aria-modal="true"
      - element has role="dialog" attribute
    Marks `is_modal=True` on each in element_map and returns the list of IDs.
    """
    modal_ids: list[int] = []
    # Map ax nodeId → ax role properties for fast lookup
    for node in ax_nodes:
        backend_id = node.get("backendDOMNodeId")
        if backend_id is None or backend_id not in element_map:
            continue
        role = _get_value(node.get("role"))
        elem = element_map[backend_id]
        if role in _MODAL_ROLES:
            elem.is_modal = True
            modal_ids.append(backend_id)
            continue
        # Check ARIA properties from the AX node
        for prop in node.get("properties", []) or []:
            name = prop.get("name", "")
            value = _get_value(prop.get("value"))
            if name == "modal" and value is True:
                elem.is_modal = True
                modal_ids.append(backend_id)
                break
        # Check explicit attribute on the DOM node
        if not elem.is_modal:
            if elem.attributes.get("role") == "dialog":
                elem.is_modal = True
                modal_ids.append(backend_id)
    return modal_ids


# ---------------------------------------------------------------------------
# Phase 1.2b: JS event-listener probing via CDP
# ---------------------------------------------------------------------------

async def _probe_js_listeners(
    cdp: Any,
    element_map: dict[int, ElementRef],
) -> None:
    """Mark non-interactive div/span/li/etc nodes interactive if they have a
    click or pointerdown listener bound via JavaScript.

    Capped at _LISTENER_PROBE_CAP nodes per snapshot — each probe is one CDP
    round trip, so unbounded probing would dominate snapshot latency on
    pages with hundreds of containers.
    """
    candidates: list[ElementRef] = []
    for elem in element_map.values():
        if elem.is_interactive:
            continue
        if elem.tag_name not in _LISTENER_PROBE_TAGS:
            continue
        if elem.bbox is None or elem.bbox[2] < 20 or elem.bbox[3] < 20:
            # Too small to be a real click target.
            continue
        candidates.append(elem)
        if len(candidates) >= _LISTENER_PROBE_CAP:
            break

    for elem in candidates:
        try:
            resolved = await cdp.send(
                "DOM.resolveNode", {"backendNodeId": elem.backend_node_id},
            )
            object_id = (resolved.get("object") or {}).get("objectId")
            if not object_id:
                continue
            listeners = await cdp.send(
                "DOMDebugger.getEventListeners", {"objectId": object_id, "depth": 0},
            )
            for ev in listeners.get("listeners", []):
                if ev.get("type") in ("click", "mousedown", "pointerdown"):
                    elem.has_js_listener = True
                    elem.is_interactive = True
                    break
        except Exception as exc:
            logger.debug("perception: getEventListeners failed for %d: %s",
                         elem.backend_node_id, exc)
            continue
