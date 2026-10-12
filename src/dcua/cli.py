"""Command line: dcua run | observe | windows | ground | mcp | doctor"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import sys

from .config import Settings


def _fix_console() -> None:
    # Windows consoles default to a legacy code page; make CJK output safe.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


async def _ainput(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


async def _confirm(text: str) -> bool:
    ans = await _ainput(f"\n⚠ 需要確認 / Confirm:\n{text}\n允許執行? [y/N] ")
    return ans.strip().lower() in {"y", "yes", "是", "好"}


async def cmd_run(s: Settings, task: str) -> int:
    from .agent import Agent, Planner
    from .executor import ComputerController

    ctrl = ComputerController(s, confirm=_confirm, task=task)
    planner = Planner(s)

    def on_event(kind: str, text: str) -> None:
        icon = {"tool": "→", "result": "  ←", "say": "💬", "check": "  ✓?"}.get(kind, "·")
        print(f"{icon} {text}")

    async def ask(q: str) -> str:
        return await _ainput(f"\n❓ {q}\n> ")

    try:
        outcome = await Agent(s, ctrl, planner, ask_user=ask, on_event=on_event).run(task)
    finally:
        await planner.aclose()
        await ctrl.aclose()
    print(f"\n{'✅' if outcome.success else '❌'} {outcome.summary}")
    print(f"steps={outcome.steps}  decision_calls={outcome.decision_calls}  "
          f"decision_cost=${outcome.decision_cost:.5f}")
    return 0 if outcome.success else 1


async def cmd_observe(s: Settings, full: bool, all_windows: bool, as_json: bool = False) -> int:
    from .executor import ComputerController

    s.injection_check = False
    ctrl = ComputerController(s)
    try:
        print(await ctrl.observe(full=full, all_windows=all_windows, fmt="json" if as_json else "text"))
    finally:
        await ctrl.aclose()
    return 0


async def cmd_windows(s: Settings) -> int:
    from .executor import ComputerController

    ctrl = ComputerController(s)
    try:
        print(await ctrl.list_windows())
    finally:
        await ctrl.aclose()
    return 0


async def cmd_ground(s: Settings, intent: str, action: str, as_json: bool = False) -> int:
    """Dry run: show which element Decision-1 would pick, with probabilities. No input is sent."""
    from .executor import ComputerController

    ctrl = ComputerController(s)
    try:
        r = await ctrl.ground(intent, action)
        if as_json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return 1 if r["status"] == "error" else 0
        if r["status"] == "error":
            print(f"error: {r['message']}")
            return 1
        print(f"status={r['status']}  p={r['probability']:.3f}  requests={r['requests']}  "
              f"risk_p={r['risk_p']:.3f}  revision={r['revision']}")
        el = r["element"]
        if el:
            print(f"pick: [{el['id']}] {el['role']} \"{el['name']}\" at {el['center']}")
        if r["alternatives"]:
            print("ranking:")
            for a in r["alternatives"]:
                print(f"  [{a['id']}] p={a['p']:.2f}  {a['describe']}")
        print(f"timing: {r['timing_ms']}")
        print(f"decision cost so far: ${ctrl.client.total_cost:.6f}")
    finally:
        await ctrl.aclose()
    return 0


async def cmd_doctor(s: Settings) -> int:
    from .decisions import Choice, DecisionClient, Noul

    ok = True
    print(f"platform        : {sys.platform}")
    print(f"backend         : {s.backend}")
    print(f"decision model  : {s.decision_model}")
    print(f"planner         : {s.planner_model or '(not set — needed only for `dcua run`)'} "
          f"@ {s.planner_base_url} [{s.planner_protocol}]")
    if sys.platform == "win32":
        if importlib.util.find_spec("uiautomation"):
            print("uiautomation    : ok")
        else:
            ok = False
            print("uiautomation    : MISSING -> pip install 'decision-cua[windows]'")
    try:
        async with DecisionClient(s.openrouter_api_key, model=s.decision_model, url=s.decision_url) as c:
            r = await c.ask("The Save As dialog shows a Save button and a Cancel button.", {
                "pick": Choice("Which button saves the file?", {"save": "Save button", "cancel": "Cancel button"}),
                "dialog": Noul("Is a dialog open?"),
            })
            print(f"decisions API   : ok  model={r.model}  {r.latency_ms:.0f} ms  "
                  f"pick={r.choice('pick').choice} p={r.choice('pick').p:.2f}  "
                  f"dialog={r.noul('dialog').p:.2f}  cost=${r.cost or 0:.6f}")
    except Exception as exc:
        ok = False
        print(f"decisions API   : FAILED -> {exc}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> None:
    _fix_console()
    ap = argparse.ArgumentParser(prog="dcua", description="Computer use for any LLM, grounded by Microsoft-Decision-1")
    ap.add_argument("--env", default=".env", help="path to .env file (default ./.env)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run an agent task with the configured planner model")
    p.add_argument("task")
    p.add_argument("--model", help="override DCUA_PLANNER_MODEL")
    p.add_argument("--protocol", choices=["tools", "json"], help="override DCUA_PLANNER_PROTOCOL")
    p.add_argument("--max-steps", type=int)

    p = sub.add_parser("observe", help="print the current screen outline")
    p.add_argument("--full", action="store_true")
    p.add_argument("--all", action="store_true", help="all windows")
    p.add_argument("--json", action="store_true", help="machine-readable JSON with revision")

    sub.add_parser("windows", help="list windows")

    p = sub.add_parser("ground", help="dry-run grounding: which element would be picked (no input sent)")
    p.add_argument("intent")
    p.add_argument("--action", default="click", choices=["click", "type", "scroll"])
    p.add_argument("--json", action="store_true", help="print the full result as JSON")

    sub.add_parser("mcp", help="start the MCP server on stdio")
    sub.add_parser("doctor", help="check configuration and call the Decisions API once")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if args.cmd == "mcp":
        from .config import load_dotenv
        from .mcp_server import main as mcp_main

        load_dotenv(args.env)
        mcp_main()
        return

    s = Settings.from_env(args.env)
    if args.cmd == "run":
        if args.model:
            s.planner_model = args.model
        if args.protocol:
            s.planner_protocol = args.protocol
        if args.max_steps:
            s.max_steps = args.max_steps
        code = asyncio.run(cmd_run(s, args.task))
    elif args.cmd == "observe":
        code = asyncio.run(cmd_observe(s, args.full, args.all, args.json))
    elif args.cmd == "windows":
        code = asyncio.run(cmd_windows(s))
    elif args.cmd == "ground":
        code = asyncio.run(cmd_ground(s, args.intent, args.action, args.json))
    else:
        code = asyncio.run(cmd_doctor(s))
    sys.exit(code)


if __name__ == "__main__":
    main()
