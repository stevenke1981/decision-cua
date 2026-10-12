"""Test doubles: a simulated Decisions API and a scripted OpenAI-compatible planner."""
from __future__ import annotations

import json
import re
from typing import Any, Callable

import httpx

WORD = re.compile(r"[\w一-鿿]+")
STOP = {"the", "a", "an", "in", "on", "of", "to", "button", "click", "into", "field", "box"}


def _words(s: str) -> set[str]:
    return {w.lower() for w in WORD.findall(s or "")} - STOP


class SimDecisions:
    """Answers like a sensible decision model, derived only from the request body."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        state = body["state"]
        answers: dict[str, Any] = {}
        for key, q in body["questions"].items():
            if q["type"] == "choice":
                answers[key] = self._choice(key, q, state)
            elif q["type"] == "noul":
                answers[key] = {"type": "noul", "noul": self._noul(key, state)}
            else:
                n = len(q["criteria"])
                answers[key] = {"type": "score", "score": n / 2,
                                "probabilities": {str(i): 1 / n for i in range(n)}, "confidence": 1 / n}
        return httpx.Response(200, json={
            "model": "microsoft/microsoft-decision-1", "answers": answers,
            "usage": {"input_tokens": 300, "output_tokens": 0, "cost": 0.0000126},
            "id": f"gen-dec-{len(self.requests)}", "provider": "Azure"})

    def _choice(self, key: str, q: dict[str, Any], state: Any) -> dict[str, Any]:
        crit: dict[str, str | None] = q["criteria"]
        if key == "target":
            intent = _words(state["step_intent"])
            window_hint = state["step_intent"].lower()
        else:
            intent = _words(json.dumps(state, ensure_ascii=False))
            window_hint = ""
        scores = {}
        for label, desc in crit.items():
            if label == "none":
                scores[label] = 0.5
                continue
            d = (desc or label)
            s = len(intent & _words(d)) * 2.0
            if "dialog" in window_hint and 'window "save as"' in d.lower():
                s += 1.5
            role = d.split(" ", 1)[0].lower()
            if role and role in window_hint:  # "...button..." matches a Button
                s += 1.0
            scores[label] = s
        best = max(scores, key=scores.get)  # type: ignore[arg-type]
        total = sum(v + 0.05 for v in scores.values())
        probs = {k: round((v + 0.05) / total, 4) for k, v in scores.items()}
        probs[best] = max(probs[best], 0.9)
        rest = 1 - probs[best]
        others = [k for k in probs if k != best]
        for k in others:
            probs[k] = round(rest / len(others), 4)
        return {"type": "choice", "choice": best, "probabilities": probs, "confidence": probs[best]}

    def _noul(self, key: str, state: Any) -> float:
        if key == "risky":
            text = json.dumps(state).lower()
            return 0.93 if ("delete" in text or "send" in text) else 0.04
        if key == "succeeded":
            return 0.08 if state["changes_detected"].get("nothing_changed") else 0.91
        if key == "injection":
            return 0.95 if any("ignore previous" in t.lower() for t in state["screen_text"]) else 0.02
        if key == "complete":
            return 0.92 if "notes.txt" in (state["screen_now"]["foreground_window"] or "") else 0.1
        return 0.5


def tool_call(name: str, args: dict[str, Any], i: int) -> dict[str, Any]:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"call_{i}", "type": "function",
         "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


class ScriptedPlanner:
    """Replays a fixed list of assistant messages; records what it was sent."""

    def __init__(self, script: list[dict[str, Any] | Callable[[list[dict[str, Any]]], dict[str, Any]]]) -> None:
        self.script = list(script)
        self.seen: list[list[dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.seen.append(body["messages"])
        step = self.script.pop(0)
        msg = step(body["messages"]) if callable(step) else step
        return httpx.Response(200, json={"choices": [{"message": msg}]})
