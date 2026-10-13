# Offline action safety validation — 2026-10-13

Source inspected: `main@86ffc34a842143882e52be82e4c118c00d81af26` in a fresh,
clean checkout of `stevenke1981/decision-cua` on host `5900X`. There was no
checkout `AGENTS.md` or `.agents` directory. The checkout skill was read.
This is a separate repository from Flow Decisions.

## Changes and regressions

| Boundary | Regression and resulting behavior |
| --- | --- |
| Risk on resolved controls | Clicking the id of `Delete all` requires confirmation; the risk request includes the resolved label, value and window. Grounding failure sends no input. |
| Foreground | Unknown foreground and a backend falsely reporting successful focus cannot authorize input. Explicit focus requests also verify the observed window handle. |
| Revision | Click, hotkey and drag recheck `if_revision` after foreground recovery. A change during recovery returns `stale` before mouse/keyboard input. Recovery itself can bring a window forward. |
| Live coordinates | A missing or empty live rectangle aborts instead of using cached coordinates. |
| Typing focus | A target must actually receive keyboard focus before clearing or typing. No detected keyboard focus stops a write. |
| Typing value | Non-submitting writes compare the readable field value with the intended append/replacement. A mismatch overrides an optimistic model result. Missing, masked or possibly truncated values remain unverified. Line breaks use the same normalization as Windows snapshots. |
| Enter and chords | `submit=true` and embedded CR/LF require confirmation even if the model calls them routine. Adding modifiers or changing key order cannot bypass the existing risky chord list. |
| Verification | Unavailable or disabled model verification returns `unverified`, while `ok=true` records that input was performed. |
| Unicode | CJK punctuation, supplementary characters and UTF-16 surrogate pairs survive batch boundaries. CRLF maps to one Enter, lone CR to Enter. Malformed Unicode is rejected before clearing/partial typing. |
| Native failures | Failed cursor positioning aborts before sending input. A failed chord-down send still attempts key-up cleanup. |

The first regression run against the original implementation produced **22 failures and
6 passes**. Additional visible-value regressions produced **2 failures and 1 pass**
before the value check was implemented. The final full Windows/Python 3.13.16 suite
produced **85 passes**, including the scripted planner and Pac-Man tests.

## Reproduce

From the checkout, with Python 3.10 or newer and uv:

```powershell
uv sync --locked --extra dev --extra mcp --extra windows
uv run --locked --extra dev ruff check src tests
uv run --locked --extra dev --extra mcp --extra windows pytest -q
uv run --locked --extra mcp python -c "import dcua.cli, dcua.mcp_server, dcua.tools"
uv run --locked python -m compileall -q src tests
git diff --check
```

The Windows dependency marker makes this command usable on Linux as well. Tests that
inspect Windows input structures skip on Linux. CI retains the Windows/Linux ×
Python 3.10/3.12/3.13 matrix and now runs locked static correctness lint (`E9`, `F63`,
`F7`, `F82`) over every source and test file. Ruff is pinned; existing runtime
dependency versions were retained.

`tests/conftest.py` replaces `ctypes.WinDLL` **before** importing the Windows module,
including import-time DPI calls. Input tests use `object.__new__(WindowsBackend)`
to skip UIA/COM initialization, replace IME and send functions, and inspect only
constructed events. Controller/planner tests use in-memory `FakeBackend` and
`httpx.MockTransport`; no real OpenRouter request is made.

On the local managed sandbox, Python's internal asyncio loopback socketpair blocked
before any action executed. The audited offline suite was run with approved shell
loopback access. This did not enable desktop input or modify host safety settings.

## Limits

- These are fixture tests, not real desktop acceptance. The official desktop route
  was previously reported unavailable; this run made no screenshot, UIA read,
  SendInput call or private clipboard access against the host. It installed no
  persistent MCP configuration and made no paid model calls.
- Actual IME behavior, UIPI, focus arbitration, UIA providers and text delivery in
  installed applications still need an available, permitted official desktop route.
- Revision comparison is optional and cannot make the final snapshot and OS input
  atomic. External focus changes after the final check, and concurrent controller
  actions, remain limitations; issue one action at a time.
- Value snapshots are bounded to 200 characters on Windows. Masked/truncated values
  cannot establish exact content, and normalization cannot distinguish line breaks
  from spaces. Submitted fields may disappear, so submission outcome still uses
  model verification rather than this local value check.
- Key-up cleanup is best effort; a failing OS input channel can still reject cleanup
  or accept only part of a batch. An error must be inspected before repeating input.
- Risk classification remains model-based outside the fixed Enter/chord rules.
  `confirm=true` remains a trusted client assertion of user approval, not an
  authentication mechanism.
