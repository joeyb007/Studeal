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
_NAME_MAX = 100
_ATTR_VALUE_MAX = 60
_TEXT_NODE_MAX = 80


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
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class PageSnapshot:
    """The thing PageReader hands to the LLM each turn."""

    text: str                              # indented tree, LLM-facing
    element_map: dict[int, ElementRef]     # backend_node_id → ElementRef
    url: str
    title: str
    char_count: int                        # for context-budget monitoring


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
        # Parallel CDP fetch — these have no inter-dependency.
        ax_data, dom_data = await asyncio.gather(
            cdp.send("Accessibility.getFullAXTree", {}),
            cdp.send("DOMSnapshot.captureSnapshot", {
                "computedStyles": ["cursor"],
                "includeDOMRects": True,
            }),
        )
    finally:
        try:
            await cdp.detach()
        except Exception:
            pass

    ax_nodes: list[dict[str, Any]] = ax_data.get("nodes", [])
    bbox_by_id, tag_by_id, attrs_by_id, cursor_by_id = _index_dom_snapshot(dom_data)

    # Build element_map from AX tree, enriching with DOMSnapshot data per node.
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

        # Visibility: must have a non-zero bbox.
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

    text = _serialize_tree(element_map, ax_nodes, ax_id_to_backend)
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

def _serialize_tree(
    element_map: dict[int, ElementRef],
    ax_nodes: list[dict[str, Any]],
    ax_id_to_backend: dict[str, int],
) -> str:
    """Indented pseudo-tree, browser-use convention.

    Format per node:
      Interactive:    [123]<tag attr="v" /> "name"
      Non-interactive: <tag /> "name"

    Nodes with no name AND no interactive descendants are pruned (noise).
    """
    # Build parent → children map keyed on backend_node_id, derived from AX
    # parent_id pointers (translated through ax_id_to_backend).
    children_of: dict[int, list[int]] = {}
    parent_of: dict[int, int] = {}

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
            parent_of[backend_id] = parent_backend

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

    def _emit(bid: int, depth: int) -> None:
        if bid not in keep:
            return
        elem = element_map[bid]
        indent = "\t" * depth
        tag = elem.tag_name or elem.role or "div"

        if elem.is_interactive:
            attrs_str = _format_attrs(elem.attributes)
            line = f"{indent}[{elem.backend_node_id}]<{tag}{attrs_str} />"
        else:
            line = f"{indent}<{tag} />"

        if elem.name:
            name_safe = _truncate(elem.name, _NAME_MAX)
            line += f' "{name_safe}"'

        lines.append(line)

        for child in children_of.get(bid, []):
            _emit(child, depth + 1)

    for root in roots:
        _emit(root, 0)

    return "\n".join(lines)


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
