"""Settings loaded from environment variables (and an optional .env file).

No python-dotenv dependency: a tiny parser is enough for KEY=VALUE files.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """Load KEY=VALUE lines into os.environ without overriding existing vars."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _as_bool(v: str) -> bool:
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # --- Decision model (Microsoft-Decision-1 via OpenRouter Decisions API) ---
    openrouter_api_key: str = ""
    decision_model: str = "microsoft/microsoft-decision-1"
    decision_url: str = "https://openrouter.ai/api/alpha/decisions"
    decision_timeout: float = 30.0

    # --- Planner (any OpenAI-compatible chat model: OpenRouter, LM Studio, Ollama, llama.cpp...) ---
    planner_base_url: str = "https://openrouter.ai/api/v1"
    planner_api_key: str = ""          # empty -> falls back to openrouter_api_key
    planner_model: str = ""
    planner_protocol: str = "tools"    # "tools" (function calling) | "json" (any text model)
    planner_vision: bool = False       # send annotated screenshots to the planner
    planner_temperature: float = 0.2

    # --- Thresholds (probe on your own apps and tune; see `dcua ground`) ---
    ground_min_prob: float = 0.55      # below -> return candidates to planner instead of acting
    risk_threshold: float = 0.5        # P(risky) >= this -> needs confirmation
    verify_threshold: float = 0.5      # P(expected outcome) < this -> report as unverified
    injection_threshold: float = 0.7   # P(screen text addresses an AI agent) >= this -> warn
    done_threshold: float = 0.35       # completion check below this -> push back once or twice

    # --- Behaviour ---
    injection_check: bool = True
    verify_actions: bool = True
    auto_confirm_risky: bool = False   # DANGEROUS: skip human confirmation
    max_options: int = 20              # options per Decision request (tournament above this)
    max_candidates: int = 160          # candidates considered for grounding
    max_elements: int = 600            # UIA elements walked per snapshot
    snapshot_budget_s: float = 4.0     # time budget for one UIA walk
    settle_s: float = 0.5              # wait after an action before re-observing
    launch_wait_s: float = 5.0         # how long launch() waits for the new app window
    outline_limit: int = 60            # elements shown to the planner in an outline observation
    max_steps: int = 40
    click_mode: str = "mouse"          # "mouse" | "invoke" | "auto"
    backend: str = "auto"              # "auto" | "windows" | "linux" | "fake"
    log_dir: str = "runs"

    @classmethod
    def from_env(cls, dotenv: str | None = ".env") -> "Settings":
        if dotenv:
            load_dotenv(dotenv)
        s = cls()
        for f in fields(cls):
            raw = os.environ.get(f"DCUA_{f.name.upper()}")
            if raw is None and f.name == "openrouter_api_key":
                raw = os.environ.get("OPENROUTER_API_KEY")
            if raw is None:
                continue
            current = getattr(s, f.name)
            if isinstance(current, bool):
                setattr(s, f.name, _as_bool(raw))
            elif isinstance(current, int):
                setattr(s, f.name, int(raw))
            elif isinstance(current, float):
                setattr(s, f.name, float(raw))
            else:
                setattr(s, f.name, raw)
        if not s.planner_api_key:
            s.planner_api_key = s.openrouter_api_key
        return s
