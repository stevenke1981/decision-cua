"""Turn UIA snapshots into compact text for LLMs and JSON state for the decision model."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

from .backends.base import Element, Rect, Snapshot

INTERACTIVE = {
    "Button", "SplitButton", "MenuItem", "Hyperlink", "CheckBox", "RadioButton", "TabItem",
    "ListItem", "TreeItem", "ComboBox", "Edit", "Document", "DataItem", "Slider", "Spinner",
    "MenuBar", "Menu", "ToolBar", "Header", "HeaderItem",
}
TYPEABLE = {"Edit", "ComboBox", "Document", "Spinner", "Custom", "Pane", "Group"}
SCROLLABLE = {"List", "Tree", "DataGrid", "Table", "Document", "Pane", "Window", "Custom", "Group"}


class IdRegistry:
    """Assign ids that stay stable across snapshots for the same element.

    Identity = (window, role, name, automation_id, path) plus an ordinal for
    duplicates (e.g. three "OK" buttons), so "e12" keeps meaning the same thing
    after the planner observed it, even if other elements appear or vanish.
    """

    def __init__(self) -> None:
        self._ids: dict[tuple[Any, ...], str] = {}
        self._next = 1

    def assign(self, snapshot: Snapshot) -> Snapshot:
        seen: Counter[tuple[str, ...]] = Counter()
        for el in snapshot.elements:
            sig = el.signature()
            key = sig + (seen[sig],)
            seen[sig] += 1
            eid = self._ids.get(key)
            if eid is None:
                eid = f"e{self._next}"
                self._next += 1
                self._ids[key] = eid
            el.id = eid
        return snapshot


# --------------------------------------------------------------------------- planner-facing text
def rank_for_outline(el: Element) -> tuple[int, int]:
    interactive = 0 if el.role in INTERACTIVE else 1
    return (interactive, el.depth)


def render_outline(snap: Snapshot, *, limit: int = 60, full: bool = False) -> str:
    lines: list[str] = []
    fg = snap.foreground
    lines.append(f"Foreground window: {fg.describe() if fg else '(none)'}")
    others = [w for w in snap.windows if not w.focused]
    if others:
        lines.append("Other windows: " + "; ".join(f'"{w.title}"' for w in others[:12])
                     + (f" (+{len(others) - 12} more)" if len(others) > 12 else ""))
    focused = snap.focused
    if focused:
        lines.append(f"Keyboard focus: [{focused.id}] {focused.describe(position=False, window=False)}")

    elements = snap.elements
    if not full:
        elements = sorted(elements, key=rank_for_outline)[:limit]
        # keep reading order (top-to-bottom, left-to-right) for the chosen subset
        elements.sort(key=lambda e: (e.rect.top // 12, e.rect.left) if e.rect else (0, 0))
    lines.append(f"Elements ({len(elements)} of {len(snap.elements)}"
                 + (", snapshot truncated" if snap.truncated else "") + "):")
    for el in elements:
        lines.append(f"  [{el.id}] {el.describe(window=False)}")
    hidden = len(snap.elements) - len(elements)
    if hidden > 0:
        lines.append(f"  ... {hidden} more elements not listed. You can still target them by "
                     "describing them in words (grounding searches all elements), or call observe(full=true).")
    return "\n".join(lines)


# --------------------------------------------------------------------------- machine-readable state
def element_dict(el: Element) -> dict[str, Any]:
    r = el.rect
    return {
        "id": el.id, "role": el.role, "name": el.name, "automation_id": el.automation_id or None,
        "value": el.value, "enabled": el.enabled, "focused": el.focused, "window": el.window,
        "path": el.path or None,
        "rect": [r.left, r.top, r.right, r.bottom] if r else None,
        "center": list(r.center) if r else None,
    }


def snapshot_revision(snap: Snapshot) -> str:
    """Short hash of what the agent can see in the foreground: window, elements, values, positions.

    Two snapshots with the same revision show the same state, so a decision made on one
    (by the agent, a human or an external reviewer) still applies to the other.
    """
    fg = snap.foreground
    # Only the foreground window and its popups: actions re-observe that scope, so a revision
    # taken from observe(all_windows=True) still matches when nothing changed in front.
    scope = {fg.title, f"{fg.title} (popup)"} if fg else set()
    payload = [[fg.handle, fg.title] if fg else None,
               [(e.role, e.name, e.automation_id, e.window, e.path, e.value, e.enabled, e.focused,
                 (e.rect.left, e.rect.top, e.rect.right, e.rect.bottom) if e.rect else None)
                for e in snap.elements if not fg or e.window in scope]]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def iso_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="milliseconds")


def snapshot_json(snap: Snapshot, *, limit: int = 60, full: bool = False) -> dict[str, Any]:
    elements = snap.elements if full else sorted(snap.elements, key=rank_for_outline)[:limit]
    fg = snap.foreground
    return {
        "revision": snapshot_revision(snap),
        "observed_at": iso_time(snap.taken_at),
        "foreground_window": ({"title": fg.title, "process": fg.process or None,
                               "rect": [fg.rect.left, fg.rect.top, fg.rect.right, fg.rect.bottom]
                               if fg.rect else None} if fg else None),
        "other_windows": [w.title for w in snap.windows if not w.focused],
        "focused_element": snap.focused.id if snap.focused else None,
        "screen": [snap.screen.left, snap.screen.top, snap.screen.right, snap.screen.bottom],
        "elements": [element_dict(e) for e in elements],
        "total_elements": len(snap.elements),
        "truncated": snap.truncated,
    }


POINT_RE = re.compile(r"^\s*\(?\s*(-?\d{1,6})\s*,\s*(-?\d{1,6})\s*\)?\s*$")


def parse_point(target: str) -> tuple[int, int] | None:
    """'640,360' or '(640, 360)' -> (640, 360); anything else -> None."""
    m = POINT_RE.match(target or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def element_at(snap: Snapshot, x: int, y: int) -> Element | None:
    """Smallest element whose box contains the point (what a click there would most likely hit)."""
    hits = [e for e in snap.elements if e.rect and e.rect.area > 1 and e.rect.contains(x, y)]
    return min(hits, key=lambda e: e.rect.area) if hits else None  # type: ignore[union-attr]


def point_element(snap: Snapshot, x: int, y: int) -> Element:
    """Pseudo element for a raw screen point, described by what lies under it.

    The description feeds the risk gate, so a point outside the foreground window says which
    window (or the desktop/taskbar) it would hit instead of looking like a harmless bare point.
    """
    fg = snap.foreground
    in_fg = fg is not None and (fg.rect is None or fg.rect.contains(x, y))
    under = element_at(snap, x, y) if in_fg else None
    if under is not None:
        return Element(id="", role="Point", name=f"({x},{y})", rect=Rect(x, y, x + 1, y + 1),
                       window=under.window, path=f"over {under.describe(position=False, window=False)}")
    # EnumWindows order is top-most first, so the first window containing the point is the one hit.
    hit = next((w for w in snap.windows if not w.minimized and w.rect and w.rect.contains(x, y)), None)
    if in_fg and fg is not None:
        where, window = f'over an area without accessible elements in window "{fg.title}"', fg.title
    elif hit is not None:
        where = (f'OUTSIDE the foreground window, over window "{hit.title}"'
                 + (f" ({hit.process})" if hit.process else ""))
        window = hit.title
    else:
        where, window = "OUTSIDE every window: on the desktop or the taskbar", ""
    return Element(id="", role="Point", name=f"({x},{y})", rect=Rect(x, y, x + 1, y + 1),
                   window=window, path=where)


# --------------------------------------------------------------------------- decision-facing state
def compact_screen(snap: Snapshot, limit: int = 45) -> dict[str, Any]:
    """Small JSON summary of the screen for verification / completion questions."""
    els = sorted(snap.elements, key=rank_for_outline)[:limit]
    return {
        "foreground_window": snap.foreground.title if snap.foreground else None,
        "open_windows": [w.title for w in snap.windows][:15],
        "focused_element": snap.focused.describe(position=False) if snap.focused else None,
        "elements": [e.describe(position=False, window=False) for e in els],
    }


def visible_text(snap: Snapshot, max_chars: int = 6000) -> list[str]:
    """Distinct visible strings (names + values) for the prompt-injection check."""
    out: list[str] = []
    seen: set[str] = set()
    total = 0
    for el in snap.elements:
        for s in (el.name, el.value or ""):
            s = s.strip()
            if len(s) < 12 or s in seen:  # short labels can't carry an injection
                continue
            seen.add(s)
            out.append(s[:400])
            total += len(s)
            if total > max_chars:
                return out
    return out


def diff(before: Snapshot, after: Snapshot, limit: int = 12) -> dict[str, Any]:
    def key(e: Element) -> tuple[str, str, str]:
        return (e.role, e.name, e.window)

    b = {key(e): e for e in before.elements}
    a = {key(e): e for e in after.elements}
    added = [a[k].describe(position=False) for k in a.keys() - b.keys()]
    removed = [b[k].describe(position=False) for k in b.keys() - a.keys()]
    changed = []
    for k in a.keys() & b.keys():
        if (a[k].value or "") != (b[k].value or ""):
            changed.append(f'{a[k].role} "{a[k].name}": "{(b[k].value or "")[:60]}" -> "{(a[k].value or "")[:60]}"')
    bw, aw = {w.title for w in before.windows}, {w.title for w in after.windows}
    out: dict[str, Any] = {
        "foreground_before": before.foreground.title if before.foreground else None,
        "foreground_after": after.foreground.title if after.foreground else None,
        "windows_opened": sorted(aw - bw)[:limit],
        "windows_closed": sorted(bw - aw)[:limit],
        "elements_added": added[:limit],
        "elements_removed": removed[:limit],
        "values_changed": changed[:limit],
        "focus_after": after.focused.describe(position=False) if after.focused else None,
    }
    out["nothing_changed"] = not any(
        out[k] for k in ("windows_opened", "windows_closed", "elements_added",
                         "elements_removed", "values_changed")
    ) and out["foreground_before"] == out["foreground_after"]
    return out


def summarize_diff(d: dict[str, Any]) -> str:
    if d.get("nothing_changed"):
        return "No visible change detected."
    bits = []
    if d["foreground_before"] != d["foreground_after"]:
        bits.append(f'foreground: "{d["foreground_before"]}" -> "{d["foreground_after"]}"')
    for label, k in (("opened", "windows_opened"), ("closed", "windows_closed")):
        if d[k]:
            bits.append(f"windows {label}: " + ", ".join(f'"{t}"' for t in d[k][:4]))
    if d["values_changed"]:
        bits.append("values: " + "; ".join(d["values_changed"][:3]))
    if d["elements_added"]:
        bits.append(f"{len(d['elements_added'])} element(s) appeared")
    if d["elements_removed"]:
        bits.append(f"{len(d['elements_removed'])} element(s) disappeared")
    return "; ".join(bits)


# --------------------------------------------------------------------------- grounding candidates
def candidates(snap: Snapshot, action: str, limit: int = 160) -> list[Element]:
    """Code-side filter: only elements that could plausibly receive `action`.

    This is a feasibility filter (visible, enabled, right kind), not a relevance
    judgment; relevance is the decision model's job.
    """
    def ok(e: Element) -> bool:
        if e.rect is None or e.rect.area <= 1:
            return False
        if action == "type":
            return e.enabled and (e.role in TYPEABLE or e.focused)
        if action == "scroll":
            return e.role in SCROLLABLE or e.role in INTERACTIVE
        return e.enabled

    pool = [e for e in snap.elements if ok(e)]
    pool.sort(key=lambda e: (0 if e.role in INTERACTIVE else 1, 0 if e.focused else 1, e.depth))
    return pool[:limit]


def chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]
