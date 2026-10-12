"""Client for OpenRouter's Decisions API (alpha), used with microsoft/microsoft-decision-1.

Wire format (POST https://openrouter.ai/api/alpha/decisions):

    {"model": "...", "state": <str | object | array>,
     "questions": {"key": {"type": "choice"|"noul"|"score", "instructions": "...", "criteria": ...}}}

Response:

    {"model": "...", "answers": {
        "key": {"type": "choice", "choice": "a", "probabilities": {"a": .9, ...}, "confidence": .9},
        "key2": {"type": "noul", "noul": 0.96},
        "key3": {"type": "score", "score": 1.4, "probabilities": {"0": .1, "1": .4, ...}, "confidence": .5}},
     "usage": {"input_tokens": 289, "output_tokens": 0, "cost": 0.000026}, "id": "...", "provider": "..."}

The model returns probabilities, never text. Code owns thresholds.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Union

import httpx

log = logging.getLogger("dcua.decisions")

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


# --------------------------------------------------------------------------- questions
@dataclass(frozen=True)
class Choice:
    """Pick exactly one option. `options` maps label -> description (or None)."""

    instructions: str
    options: Mapping[str, str | None]

    def to_wire(self) -> dict[str, Any]:
        if len(self.options) < 2:
            raise ValueError("a choice question needs at least two options")
        return {"type": "choice", "instructions": self.instructions, "criteria": dict(self.options)}


@dataclass(frozen=True)
class Noul:
    """Probability that a condition holds (yes/no)."""

    instructions: str
    true: str | None = None
    false: str | None = None

    def to_wire(self) -> dict[str, Any]:
        q: dict[str, Any] = {"type": "noul", "instructions": self.instructions}
        if self.true and self.false:
            q["criteria"] = {"true": self.true, "false": self.false}
        return q


@dataclass(frozen=True)
class Score:
    """Position on an ordered scale (levels listed low -> high)."""

    instructions: str
    levels: tuple[str, ...]

    def to_wire(self) -> dict[str, Any]:
        if len(self.levels) < 2:
            raise ValueError("a score question needs at least two levels")
        return {"type": "score", "instructions": self.instructions, "criteria": list(self.levels)}


Question = Union[Choice, Noul, Score]


# --------------------------------------------------------------------------- answers
@dataclass
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float]
    confidence: float

    @property
    def p(self) -> float:
        return self.probabilities.get(self.choice, self.confidence)

    def ranked(self) -> list[tuple[str, float]]:
        return sorted(self.probabilities.items(), key=lambda kv: kv[1], reverse=True)


@dataclass
class NoulAnswer:
    p: float

    @property
    def yes(self) -> bool:
        return self.p >= 0.5


@dataclass
class ScoreAnswer:
    score: float
    probabilities: list[float]
    confidence: float | None


Answer = Union[ChoiceAnswer, NoulAnswer, ScoreAnswer]


@dataclass
class DecisionResult:
    answers: dict[str, Answer]
    model: str = ""
    latency_ms: float = 0.0
    cost: float | None = None
    request_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def choice(self, key: str) -> ChoiceAnswer:
        a = self.answers[key]
        assert isinstance(a, ChoiceAnswer), f"{key} is not a choice answer"
        return a

    def noul(self, key: str) -> NoulAnswer:
        a = self.answers[key]
        assert isinstance(a, NoulAnswer), f"{key} is not a noul answer"
        return a

    def score(self, key: str) -> ScoreAnswer:
        a = self.answers[key]
        assert isinstance(a, ScoreAnswer), f"{key} is not a score answer"
        return a


class DecisionError(RuntimeError):
    pass


# --------------------------------------------------------------------------- client
class DecisionClient:
    """Async client. One instance per process; reuse it (keeps the HTTP connection warm)."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "microsoft/microsoft-decision-1",
        url: str = "https://openrouter.ai/api/alpha/decisions",
        timeout: float = 30.0,
        max_retries: int = 3,
        session_id: str | None = None,
        app_title: str = "decision-cua",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._has_key = bool(api_key)
        self.model = model
        self.url = url
        self.max_retries = max_retries
        self.session_id = session_id
        self.total_cost = 0.0
        self.calls = 0
        self._http = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Title": app_title,
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "DecisionClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def ask(self, state: Any, questions: Mapping[str, Question]) -> DecisionResult:
        if not self._has_key:
            raise DecisionError("OPENROUTER_API_KEY is not set")
        if not questions:
            raise ValueError("at least one question is required")
        body: dict[str, Any] = {
            "model": self.model,
            "state": state,
            "questions": {k: q.to_wire() for k, q in questions.items()},
        }
        if self.session_id:
            body["session_id"] = self.session_id

        last_err: str = ""
        for attempt in range(self.max_retries + 1):
            t0 = time.perf_counter()
            try:
                resp = await self._http.post(self.url, json=body)
            except httpx.TransportError as exc:
                last_err = f"transport error: {exc!r}"
            else:
                if resp.status_code < 400:
                    latency = (time.perf_counter() - t0) * 1000
                    try:
                        payload = resp.json()
                    except ValueError as exc:
                        raise DecisionError("Decisions API returned invalid JSON") from exc
                    result = self._parse(payload, questions, latency)
                    self.calls += 1
                    self.total_cost += result.cost or 0.0
                    log.debug("decision %s in %.0f ms (cost=%s)", list(questions), latency, result.cost)
                    return result
                last_err = f"HTTP {resp.status_code}: {resp.text[:400]}"
                if resp.status_code not in RETRY_STATUS:
                    raise DecisionError(last_err)
            if attempt < self.max_retries:
                await asyncio.sleep(min(8.0, 0.5 * 2**attempt) + random.random() * 0.25)
        raise DecisionError(f"Decisions API failed after retries: {last_err}")

    # ------------------------------------------------------------------ parsing
    @staticmethod
    def _parse(payload: Any, questions: Mapping[str, Question], latency_ms: float) -> DecisionResult:
        if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
            raise DecisionError(f"unexpected response shape: {str(payload)[:300]}")
        raw_answers = payload["answers"]
        answers: dict[str, Answer] = {}
        for key, q in questions.items():
            a = raw_answers.get(key)
            if not isinstance(a, dict):
                raise DecisionError(f"missing answer for question {key!r}")
            if isinstance(q, Choice):
                choice = a.get("choice")
                if choice not in q.options:
                    raise DecisionError(f"{key}: model chose unsupported option {choice!r}")
                probs = {str(k): float(v) for k, v in (a.get("probabilities") or {}).items()}
                conf = a.get("confidence")
                conf_f = float(conf) if conf is not None else probs.get(choice, 1.0)
                answers[key] = ChoiceAnswer(choice=choice, probabilities=probs, confidence=conf_f)
            elif isinstance(q, Noul):
                if "noul" not in a:
                    raise DecisionError(f"{key}: noul answer missing")
                answers[key] = NoulAnswer(p=float(a["noul"]))
            else:  # Score: probabilities keyed by index strings "0", "1", ...
                raw_p = a.get("probabilities") or {}
                probs_l = [float(raw_p.get(str(i), 0.0)) for i in range(len(q.levels))]
                conf = a.get("confidence")
                answers[key] = ScoreAnswer(
                    score=float(a.get("score", 0.0)),
                    probabilities=probs_l,
                    confidence=float(conf) if conf is not None else None,
                )
        usage = payload.get("usage") or {}
        cost = usage.get("cost")
        return DecisionResult(
            answers=answers,
            model=str(payload.get("model", "")),
            latency_ms=latency_ms,
            cost=float(cost) if cost is not None else None,
            request_id=str(payload.get("id", "")),
            raw=payload,
        )
