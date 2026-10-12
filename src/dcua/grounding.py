"""Map a natural-language step intent onto one UI element with Microsoft-Decision-1.

The planner LLM says *what* ("click the Save button in the dialog"); this module
decides *which element*, with a calibrated probability. When there are more
candidates than fit one request, chunks are scored in parallel and the chunk
winners meet in a final round (a tournament).
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from .backends.base import Element, Snapshot
from .decisions import Choice, DecisionClient
from .observe import candidates, chunks

ID_RE = re.compile(r"^\s*\[?(e\d+)\]?\s*$", re.IGNORECASE)
NONE = "none"
VERBS = {"click": "click", "type": "type into", "scroll": "scroll", "hover": "point at"}


@dataclass
class GroundingResult:
    element: Element | None
    probability: float
    status: str                      # ok | low_confidence | no_match | no_candidates | direct
    alternatives: list[tuple[Element, float]] = field(default_factory=list)
    requests: int = 0

    def explain(self, limit: int = 4) -> str:
        if not self.alternatives:
            return ""
        return "\n".join(f"  [{e.id}] p={p:.2f}  {e.describe(window=False)}"
                         for e, p in self.alternatives[:limit])


def parse_element_ref(target: str) -> str | None:
    m = ID_RE.match(target or "")
    return m.group(1).lower() if m else None


class Grounder:
    def __init__(self, client: DecisionClient, *, max_options: int = 20, max_candidates: int = 160,
                 min_prob: float = 0.55) -> None:
        self.client = client
        self.max_options = max(2, max_options)
        self.max_candidates = max_candidates
        self.min_prob = min_prob

    async def ground(self, intent: str, action: str, snap: Snapshot, task: str = "") -> GroundingResult:
        ref = parse_element_ref(intent)
        if ref:
            el = snap.by_id(ref)
            if el is None:
                return GroundingResult(None, 0.0, "no_match")
            return GroundingResult(el, 1.0, "direct")

        pool = candidates(snap, action, self.max_candidates)
        if not pool:
            return GroundingResult(None, 0.0, "no_candidates")

        requests = 0
        contenders: list[tuple[Element, float]] = []
        round_pool = pool
        while True:
            groups = list(chunks(round_pool, self.max_options))
            results = await asyncio.gather(*(self._round(g, intent, action, snap, task) for g in groups))
            requests += len(groups)
            contenders = []
            final_ranking: list[tuple[Element, float]] = []
            for group, ranking in zip(groups, results):
                if ranking and ranking[0][0] is not None:
                    contenders.append((ranking[0][0], ranking[0][1]))
                if len(groups) == 1:
                    final_ranking = [(e, p) for e, p in ranking if e is not None]
            if not contenders:
                return GroundingResult(None, 0.0, "no_match", requests=requests)
            if len(groups) == 1:
                best, p = contenders[0]
                status = "ok" if p >= self.min_prob else "low_confidence"
                return GroundingResult(best, p, status, final_ranking, requests)
            if len(contenders) == 1:
                best, p = contenders[0]
                status = "ok" if p >= self.min_prob else "low_confidence"
                return GroundingResult(best, p, status, contenders, requests)
            round_pool = [e for e, _ in contenders]   # next round: the chunk winners

    async def _round(self, group: list[Element], intent: str, action: str, snap: Snapshot,
                     task: str) -> list[tuple[Element | None, float]]:
        options: dict[str, str | None] = {e.id: e.describe() for e in group}
        options[NONE] = "None of the listed elements is the one step_intent refers to."
        verb = VERBS.get(action, action)
        state = {
            "overall_task": task or None,
            "step_intent": intent,
            "action": action,
            "foreground_window": snap.foreground.title if snap.foreground else None,
            "keyboard_focus": snap.focused.describe(position=False) if snap.focused else None,
        }
        q = Choice(
            instructions=(
                f"Which on-screen element should the agent {verb} to carry out `step_intent` exactly? "
                "Match the element's role, label, location and window to what `step_intent` describes; "
                "an element with a similar label but in the wrong window or of the wrong kind does not "
                f"match. Answer {NONE} if no listed element fits."
            ),
            options=options,
        )
        res = await self.client.ask(state, {"target": q})
        ans = res.choice("target")
        by_id = {e.id: e for e in group}
        ranking: list[tuple[Element | None, float]] = []
        for label, p in ans.ranked():
            ranking.append((by_id.get(label), p))
        # put the model's chosen option first even if probabilities tie
        ranking.sort(key=lambda t: (t[0] is not None and t[0].id == ans.choice, t[1]), reverse=True)
        if ans.choice == NONE:
            return [(None, ans.p)]
        return ranking
