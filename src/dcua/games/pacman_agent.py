"""Policies that pick Pac-Man's next move, plus a headless game loop.

DecisionPolicy asks Microsoft-Decision-1 one Choice question per tick: the options are the
legal directions and the answer carries a probability for each, which the UI draws as arrows.
HeuristicPolicy produces the same shape of answer offline (no API key, no cost).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Optional, Protocol

from ..decisions import Choice, DecisionClient, DecisionError
from .pacman import INF, OPPOSITE, MoveInfo, PacmanGame

log = logging.getLogger("dcua.games.pacman")

INSTRUCTIONS = (
    "You are playing Pac-Man. Pick Pac-Man's next move. Eat pellets quickly, never move onto "
    "or next to a dangerous ghost, prefer moves that keep dangerous ghosts far away, and chase "
    "frightened (edible) ghosts while they are close."
)
HEURISTIC_TEMPERATURE = 2.0


@dataclass
class MoveDecision:
    move: str
    probabilities: Dict[str, float]
    source: str               # "decision-1" | "heuristic" | "forced" | "fallback"
    latency_ms: float = 0.0
    cost: Optional[float] = None
    error: str = ""

    @property
    def p(self) -> float:
        return self.probabilities.get(self.move, 0.0)


class Policy(Protocol):
    name: str

    async def decide(self, game: PacmanGame) -> MoveDecision: ...


def describe_move(info: MoveInfo) -> str:
    """One option's criteria text: what happens if Pac-Man moves this way."""
    x, y = info.target
    parts = [f"move {info.direction} to x={x}, y={y}"]
    if info.power:
        parts.append("eats a power pellet")
    elif info.pellet:
        parts.append("eats a pellet")
    elif info.pellet_dist < INF:
        parts.append(f"nearest pellet {info.pellet_dist} steps away")
    if info.ghost_dist == 0:
        parts.append("runs INTO a dangerous ghost")
    elif info.ghost_dist < INF:
        parts.append(f"nearest dangerous ghost {info.ghost_dist} steps away")
    else:
        parts.append("no dangerous ghost can reach")
    if info.edible_dist < INF:
        parts.append(f"edible ghost {info.edible_dist} steps away")
    return ", ".join(parts)


def _forced(game: PacmanGame) -> Optional[MoveDecision]:
    legal = game.legal_moves()
    if len(legal) > 1:
        return None
    move = legal[0] if legal else game.pac_dir
    return MoveDecision(move, {move: 1.0} if legal else {}, "forced")


def heuristic_scores(game: PacmanGame) -> Dict[str, float]:
    scores = {}
    for d in game.legal_moves():
        info = game.move_info(d)
        s = 0.0
        if info.ghost_dist == 0:
            s -= 100
        elif info.ghost_dist <= 2:
            s -= 40 / info.ghost_dist
        elif info.ghost_dist <= 4:
            s -= 5
        s += 6 if info.power else 3 if info.pellet else 0
        s -= 0.3 * min(info.pellet_dist, 30)
        if info.edible_dist < INF:
            s += max(0, 8 - info.edible_dist)
        if d == OPPOSITE[game.pac_dir]:
            s -= 0.5  # mild preference for keeping momentum
        scores[d] = s
    return scores


def softmax(scores: Dict[str, float], temperature: float = HEURISTIC_TEMPERATURE) -> Dict[str, float]:
    if not scores:
        return {}
    top = max(scores.values())
    exp = {k: math.exp((v - top) / temperature) for k, v in scores.items()}
    total = sum(exp.values())
    return {k: v / total for k, v in exp.items()}


class HeuristicPolicy:
    name = "HEURISTIC"

    async def decide(self, game: PacmanGame) -> MoveDecision:
        forced = _forced(game)
        if forced:
            return forced
        probs = softmax(heuristic_scores(game))
        return MoveDecision(max(probs, key=probs.__getitem__), probs, "heuristic")


class DecisionPolicy:
    """Microsoft-Decision-1 picks the move; on API errors the heuristic covers that tick."""

    name = "MICROSOFT DECISION 1"

    def __init__(self, client: DecisionClient, fallback: Optional[Policy] = None) -> None:
        self.client = client
        self.fallback = fallback if fallback is not None else HeuristicPolicy()

    async def decide(self, game: PacmanGame) -> MoveDecision:
        forced = _forced(game)
        if forced:
            return forced
        legal = game.legal_moves()
        question = Choice(INSTRUCTIONS, {d: describe_move(game.move_info(d)) for d in legal})
        try:
            r = await self.client.ask(game.to_text(), {"move": question})
        except DecisionError as exc:
            log.warning("Decision-1 failed, using fallback: %s", exc)
            d = await self.fallback.decide(game)
            d.source, d.error = "fallback", str(exc)
            return d
        a = r.choice("move")
        probs = {d: a.probabilities.get(d, 0.0) for d in legal}
        return MoveDecision(a.choice, probs, "decision-1", r.latency_ms, r.cost)


@dataclass
class GameSummary:
    score: int
    steps: int
    won: bool
    lives: int
    model_calls: int = 0
    cost: float = 0.0
    latencies: list = field(default_factory=list)

    @property
    def avg_latency_ms(self) -> float:
        return sum(self.latencies) / len(self.latencies) if self.latencies else 0.0


StepHook = Callable[[int, MoveDecision, PacmanGame], Optional[Awaitable[None]]]


async def play(game: PacmanGame, policy: Policy, max_steps: int = 600,
               on_step: Optional[StepHook] = None) -> GameSummary:
    """Run the game without a window until it ends or `max_steps` ticks pass."""
    summary = GameSummary(score=0, steps=0, won=False, lives=game.lives)
    while not game.over and summary.steps < max_steps:
        decision = await policy.decide(game)
        if decision.source == "decision-1":
            summary.model_calls += 1
            summary.cost += decision.cost or 0.0
            summary.latencies.append(decision.latency_ms)
        game.step(decision.move)
        summary.steps += 1
        if on_step is not None:
            maybe = on_step(summary.steps, decision, game)
            if maybe is not None:
                await maybe
    summary.score, summary.won, summary.lives = game.score, game.won, game.lives
    return summary
