"""observe(json) + revision, ground dry run, if_revision, point targets, drag, timing, refocus."""
import asyncio
import json
from datetime import datetime

import httpx

from dcua.backends.fake import FakeBackend
from dcua.config import Settings
from dcua.decisions import DecisionClient
from dcua.executor import ComputerController
from dcua.observe import parse_point

from sim import SimDecisions


def make(tmp_path):
    s = Settings(openrouter_api_key="k", settle_s=0, log_dir=str(tmp_path), backend="fake")
    backend = FakeBackend()
    client = DecisionClient("k", transport=httpx.MockTransport(SimDecisions().handler))
    return backend, ComputerController(s, client=client, backend_factory=lambda: backend)


def run(ctrl, coro_fn):
    async def go():
        try:
            return await coro_fn()
        finally:
            await ctrl.aclose()
    return asyncio.run(go())


def test_observe_json_has_ids_rects_and_a_stable_revision(tmp_path):
    backend, ctrl = make(tmp_path)

    async def flow():
        a = json.loads(await ctrl.observe(fmt="json"))
        b = json.loads(await ctrl.observe(fmt="json"))
        await ctrl.type_text("hello")
        c = json.loads(await ctrl.observe(fmt="json"))
        text = await ctrl.observe()
        return a, b, c, text

    a, b, c, text = run(ctrl, flow)
    assert a["foreground_window"]["title"] == "Untitled - Notepad"
    save = next(e for e in a["elements"] if e["name"] == "Save")
    assert save["id"].startswith("e") and save["rect"] == [210, 80, 260, 100] and save["center"] == [235, 90]
    assert datetime.fromisoformat(a["observed_at"]).tzinfo is not None, "timestamp must carry a timezone"
    assert a["revision"] == b["revision"], "same screen must give the same revision"
    assert c["revision"] != a["revision"], "typing changes the editor value, so the revision changes"
    assert f"revision: {c['revision']}" in text


def test_ground_is_a_dry_run(tmp_path):
    backend, ctrl = make(tmp_path)
    r = run(ctrl, lambda: ctrl.ground("the Save button in the toolbar"))
    assert backend.log == [], "ground must not send any input"
    assert r["element"]["name"] == "Save" and r["would_act"] and not r["needs_confirmation"]
    assert r["risk_p"] < 0.5 and len(r["revision"]) == 16 and "decide" in r["timing_ms"]


def test_ground_flags_a_risky_target(tmp_path):
    backend, ctrl = make(tmp_path)
    r = run(ctrl, lambda: ctrl.ground("the Delete all button"))
    assert r["element"]["name"] == "Delete all" and r["needs_confirmation"] and backend.log == []


def test_stale_revision_blocks_the_action(tmp_path):
    backend, ctrl = make(tmp_path)

    async def flow():
        rev = json.loads(await ctrl.observe(fmt="json"))["revision"]
        await ctrl.type_text("changed")                     # screen moves on
        stale = await ctrl.click("the Save button in the toolbar", if_revision=rev)
        fresh = json.loads(await ctrl.observe(fmt="json"))["revision"]
        ok = await ctrl.click("the Save button in the toolbar", if_revision=fresh)
        return stale, ok

    stale, ok = run(ctrl, flow)
    assert stale.status == "stale" and not stale.ok and "Nothing was done" in stale.message
    assert ok.status == "done" and any(w.title == "Save As" for w in backend.windows)
    assert sum(1 for line in backend.log if line.startswith("click")) == 1, "only the fresh click ran"


def test_point_target_clicks_what_is_under_it(tmp_path):
    backend, ctrl = make(tmp_path)
    res = run(ctrl, lambda: ctrl.click("235,90", expect="a Save As dialog opens"))
    assert res.status == "done", res.to_text()
    assert backend.log[0] == "click left (235,90) -> Save"
    assert 'over Button "Save"' in res.target and res.grounding_p == 1.0


def test_point_outside_the_screen_is_not_found(tmp_path):
    backend, ctrl = make(tmp_path)
    res = run(ctrl, lambda: ctrl.click("(5000, 5000)"))
    assert res.status == "not_found" and backend.log == []


def test_drag_between_points_and_timing(tmp_path):
    backend, ctrl = make(tmp_path)
    res = run(ctrl, lambda: ctrl.drag("200,300", "400,500"))
    assert res.ok and backend.log == ["drag left (200,300) -> (400,500)"]
    assert {"observe", "decide", "act", "settle", "verify", "total"} <= res.timing_ms.keys()
    assert "timing: " in res.to_text() and f"revision: {res.revision}" in res.to_text()


def test_refocuses_the_observed_window_if_another_app_took_the_foreground(tmp_path):
    backend, ctrl = make(tmp_path)

    async def flow():
        snap = await ctrl.snapshot()
        save = next(e for e in snap.elements if e.name == "Save")
        backend.fg = 2                                       # Explorer steals the foreground
        await ctrl._ensure_foreground(save, snap)

    run(ctrl, flow)
    assert backend.fg == 1 and backend.log == ["focus Untitled - Notepad"]


def test_parse_point():
    assert parse_point("640,360") == (640, 360)
    assert parse_point(" (12, -4) ") == (12, -4)
    assert parse_point("e12") is None and parse_point("the Save button") is None


class BackgroundLaunchBackend(FakeBackend):
    """Opens the launched app's window *behind* the current one, like Windows' foreground lock."""

    def launch(self, target: str) -> bool:
        super().launch(target)
        from dcua.backends.fake import FakeWindow
        from dcua.backends.base import Rect
        self.windows.append(FakeWindow(9, "Untitled - Paint", "mspaint.exe", Rect(0, 0, 800, 600)))
        return True


def test_launch_brings_the_new_window_to_the_foreground(tmp_path):
    s = Settings(openrouter_api_key="k", settle_s=0, log_dir=str(tmp_path), backend="fake")
    backend = BackgroundLaunchBackend()
    client = DecisionClient("k", transport=httpx.MockTransport(SimDecisions().handler))
    ctrl = ComputerController(s, client=client, backend_factory=lambda: backend)
    res = run(ctrl, lambda: ctrl.launch("mspaint"))
    assert res.status == "done" and backend.fg == 9 and '"Untitled - Paint" is in the foreground' in res.message


def test_launch_without_a_new_window_says_so(tmp_path):
    s = Settings(openrouter_api_key="k", settle_s=0, launch_wait_s=0, log_dir=str(tmp_path), backend="fake")
    backend = FakeBackend()
    client = DecisionClient("k", transport=httpx.MockTransport(SimDecisions().handler))
    ctrl = ComputerController(s, client=client, backend_factory=lambda: backend)
    res = run(ctrl, lambda: ctrl.launch("https://example.com"))
    assert res.ok and "no new window appeared" in res.message and backend.fg == 1


# ----------------------------------------------------------------------------- review follow-ups
def test_risky_point_click_still_needs_confirmation(tmp_path):
    backend, ctrl = make(tmp_path)
    res = run(ctrl, lambda: ctrl.click("310,90"))            # centre of "Delete all"
    assert res.status == "needs_confirmation" and backend.log == []


def test_point_outside_every_window_is_described_for_the_risk_gate(tmp_path):
    backend, ctrl = make(tmp_path)
    r = run(ctrl, lambda: ctrl.ground("1500,900"))
    assert "OUTSIDE every window" in r["element"]["path"] and backend.log == []


def test_refocus_failure_aborts_without_input(tmp_path):
    backend, ctrl = make(tmp_path)
    backend.focus_window = lambda window: False              # the OS refuses to switch back

    async def flow():
        snap = await ctrl.snapshot()
        backend.fg = 2
        try:
            await ctrl._ensure_foreground(next(e for e in snap.elements if e.name == "Save"), snap)
        except RuntimeError as exc:
            return str(exc)
        return None

    msg = run(ctrl, flow)
    assert msg and "could not be brought back" in msg and not any(l.startswith("click") for l in backend.log)


def test_keyboard_actions_refocus_the_observed_window(tmp_path):
    backend, ctrl = make(tmp_path)
    real_risk = ctrl.guard.risk

    async def risk_then_steal(*a, **kw):                     # another app grabs focus while deciding
        p = await real_risk(*a, **kw)
        backend.fg = 2
        return p

    ctrl.guard.risk = risk_then_steal
    run(ctrl, lambda: ctrl.type_text("abc"))
    assert backend.log[0] == "focus Untitled - Notepad" and backend.editor_text == "abc"


def test_revision_is_rechecked_right_before_input(tmp_path):
    backend, ctrl = make(tmp_path)
    real_risk = ctrl.guard.risk

    async def risk_then_change(*a, **kw):                    # screen changes during the decision
        p = await real_risk(*a, **kw)
        backend._win(1).widgets[4].value = "edited meanwhile"
        return p

    async def flow():
        rev = json.loads(await ctrl.observe(fmt="json"))["revision"]
        ctrl.guard.risk = risk_then_change
        return await ctrl.click("the Save button in the toolbar", if_revision=rev)

    res = run(ctrl, flow)
    assert res.status == "stale" and not any(l.startswith("click") for l in backend.log)


def test_if_revision_on_hotkey_and_drag(tmp_path):
    backend, ctrl = make(tmp_path)

    async def flow():
        return (await ctrl.hotkey("ctrl+s", if_revision="0000000000000000"),
                await ctrl.drag("200,300", "400,500", if_revision="0000000000000000"))

    hk, dr = run(ctrl, flow)
    assert hk.status == dr.status == "stale" and backend.log == []


def test_all_windows_revision_matches_foreground_actions(tmp_path):
    backend, ctrl = make(tmp_path)

    async def flow():
        rev = json.loads(await ctrl.observe(all_windows=True, fmt="json"))["revision"]
        return await ctrl.click("the Save button in the toolbar", if_revision=rev)

    assert run(ctrl, flow).status == "done"


def test_bad_format_and_action_return_errors_not_exceptions(tmp_path):
    backend, ctrl = make(tmp_path)

    async def flow():
        return await ctrl.observe(fmt="xml"), await ctrl.ground("Save", action="hover")

    text, g = run(ctrl, flow)
    assert text.startswith("[error]") and g["status"] == "error"


class SplashLaunchBackend(FakeBackend):
    def launch(self, target: str) -> bool:
        from dcua.backends.base import Rect
        from dcua.backends.fake import FakeWindow
        self.windows.append(FakeWindow(8, "Update available", "updater.exe", Rect(0, 0, 300, 100)))
        self.windows.append(FakeWindow(9, "Untitled - Paint", "mspaint.exe", Rect(0, 0, 800, 600)))
        return True


def test_launch_prefers_the_window_of_the_launched_process(tmp_path):
    s = Settings(openrouter_api_key="k", settle_s=0, log_dir=str(tmp_path), backend="fake")
    backend = SplashLaunchBackend()
    client = DecisionClient("k", transport=httpx.MockTransport(SimDecisions().handler))
    ctrl = ComputerController(s, client=client, backend_factory=lambda: backend)
    res = run(ctrl, lambda: ctrl.launch(r"C:\Windows\System32\mspaint.exe"))
    assert backend.fg == 9 and res.target == "Untitled - Paint"
