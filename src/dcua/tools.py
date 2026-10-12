"""Tool catalogue shared by the built-in agent loop (OpenAI tool schema) and docs."""
from __future__ import annotations

import json
from typing import Any

from .executor import ComputerController

TARGET_DOC = ("Either an element id from observe() like 'e12', a precise natural-language "
              "description, e.g. 'the Save button in the Save As dialog', or screen coordinates 'x,y' "
              "(from screenshot) for surfaces without accessible elements such as canvases.")
EXPECT_DOC = "Optional: what should be visible afterwards (used to verify the step)."
REVISION_DOC = ("Optional: the revision from observe/ground/a previous result that this step was decided "
                "on. If the screen changed since, nothing is done and status is 'stale'.")

TOOL_SPECS: list[dict[str, Any]] = [
    {"name": "observe",
     "description": "Read the current screen as text: foreground window, focus, and interactive elements with ids. Call this first and whenever unsure.",
     "parameters": {"type": "object", "properties": {
         "full": {"type": "boolean", "description": "List every element instead of the top ones."},
         "all_windows": {"type": "boolean", "description": "Include elements of all windows (slow)."},
         "format": {"type": "string", "enum": ["text", "json"],
                    "description": "json = machine-readable (ids, rects, values, revision, observed_at)."}}}},
    {"name": "ground",
     "description": "Dry run: which element a target resolves to, alternatives, and risk. Sends no input.",
     "parameters": {"type": "object", "properties": {
         "target": {"type": "string", "description": TARGET_DOC},
         "action": {"type": "string", "enum": ["click", "type", "scroll"]}}, "required": ["target"]}},
    {"name": "list_windows", "description": "List open top-level windows.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "focus_window", "description": "Bring a window to the front by (part of) its title or a description.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "launch", "description": "Start an app, open a file, folder or URL (e.g. 'notepad', 'ms-settings:', 'https://...').",
     "parameters": {"type": "object", "properties": {"app": {"type": "string"},
                    "confirm": {"type": "boolean"}}, "required": ["app"]}},
    {"name": "click", "description": "Click a UI element.",
     "parameters": {"type": "object", "properties": {
         "target": {"type": "string", "description": TARGET_DOC},
         "button": {"type": "string", "enum": ["left", "right", "middle"]},
         "double": {"type": "boolean"},
         "expect": {"type": "string", "description": EXPECT_DOC},
         "confirm": {"type": "boolean", "description": "Set true only after the user approved a consequential action."},
         "if_revision": {"type": "string", "description": REVISION_DOC}},
         "required": ["target"]}},
    {"name": "drag", "description": "Press on start, move to end, release (draw, select a range, move a slider or item).",
     "parameters": {"type": "object", "properties": {
         "start": {"type": "string", "description": TARGET_DOC},
         "end": {"type": "string", "description": TARGET_DOC},
         "button": {"type": "string", "enum": ["left", "right", "middle"]},
         "expect": {"type": "string", "description": EXPECT_DOC},
         "confirm": {"type": "boolean"},
         "if_revision": {"type": "string", "description": REVISION_DOC}}, "required": ["start", "end"]}},
    {"name": "type_text", "description": "Type text (any language). Without target it types into the focused element.",
     "parameters": {"type": "object", "properties": {
         "text": {"type": "string"},
         "target": {"type": "string", "description": TARGET_DOC},
         "clear": {"type": "boolean", "description": "Clear the field first."},
         "submit": {"type": "boolean", "description": "Press Enter afterwards."},
         "expect": {"type": "string", "description": EXPECT_DOC},
         "confirm": {"type": "boolean"},
         "if_revision": {"type": "string", "description": REVISION_DOC}}, "required": ["text"]}},
    {"name": "hotkey", "description": "Press a key or chord, e.g. 'ctrl+s', 'alt+tab', 'enter', 'win+e'.",
     "parameters": {"type": "object", "properties": {
         "keys": {"type": "string"}, "expect": {"type": "string", "description": EXPECT_DOC},
         "confirm": {"type": "boolean"},
         "if_revision": {"type": "string", "description": REVISION_DOC}}, "required": ["keys"]}},
    {"name": "scroll", "description": "Scroll the foreground window or a target element.",
     "parameters": {"type": "object", "properties": {
         "direction": {"type": "string", "enum": ["up", "down"]},
         "amount": {"type": "integer", "description": "Wheel notches (default 5)."},
         "target": {"type": "string", "description": TARGET_DOC}}}},
    {"name": "wait", "description": "Wait for the UI (seconds, max 30).",
     "parameters": {"type": "object", "properties": {"seconds": {"type": "number"}}, "required": ["seconds"]}},
]

AGENT_ONLY_SPECS: list[dict[str, Any]] = [
    {"name": "screenshot", "description": "Get an annotated screenshot (boxes labelled with element ids).",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "ask_user", "description": "Ask the human a question when blocked or when a decision is theirs.",
     "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
    {"name": "done", "description": "Finish. Summarise what was done (or why it could not be done).",
     "parameters": {"type": "object", "properties": {
         "summary": {"type": "string"}, "success": {"type": "boolean"}}, "required": ["summary"]}},
]


def openai_tools(vision: bool) -> list[dict[str, Any]]:
    specs = TOOL_SPECS + [s for s in AGENT_ONLY_SPECS if vision or s["name"] != "screenshot"]
    return [{"type": "function", "function": s} for s in specs]


async def dispatch(ctrl: ComputerController, name: str, args: dict[str, Any]) -> str:
    """Execute a computer tool and return text for the model."""
    a = {k: v for k, v in (args or {}).items() if v is not None}
    if name == "observe":
        return await ctrl.observe(full=bool(a.get("full")), all_windows=bool(a.get("all_windows")),
                                  fmt=a.get("format", "text"))
    if name == "ground":
        return json.dumps(await ctrl.ground(str(a["target"]), a.get("action", "click")), ensure_ascii=False)
    if name == "list_windows":
        return await ctrl.list_windows()
    if name == "focus_window":
        return (await ctrl.focus_window(str(a["query"]))).to_text()
    if name == "launch":
        return (await ctrl.launch(str(a["app"]), confirm=bool(a.get("confirm")))).to_text()
    if name == "click":
        return (await ctrl.click(str(a["target"]), button=a.get("button", "left"),
                                 double=bool(a.get("double")), expect=a.get("expect"),
                                 confirm=bool(a.get("confirm")), if_revision=a.get("if_revision"))).to_text()
    if name == "drag":
        return (await ctrl.drag(str(a["start"]), str(a["end"]), button=a.get("button", "left"),
                                expect=a.get("expect"), confirm=bool(a.get("confirm")),
                                if_revision=a.get("if_revision"))).to_text()
    if name == "type_text":
        return (await ctrl.type_text(str(a["text"]), target=a.get("target"), clear=bool(a.get("clear")),
                                     submit=bool(a.get("submit")), expect=a.get("expect"),
                                     confirm=bool(a.get("confirm")), if_revision=a.get("if_revision"))).to_text()
    if name == "hotkey":
        return (await ctrl.hotkey(str(a["keys"]), expect=a.get("expect"),
                                  confirm=bool(a.get("confirm")), if_revision=a.get("if_revision"))).to_text()
    if name == "scroll":
        return (await ctrl.scroll(a.get("direction", "down"), int(a.get("amount", 5)),
                                  a.get("target"))).to_text()
    if name == "wait":
        return (await ctrl.wait(float(a.get("seconds", 1)))).to_text()
    return f"[error] unknown tool {name!r}"
