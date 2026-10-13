"""Regressions against an in-memory desktop and offline decision transport."""
import json

import pytest

from dcua.decisions import DecisionError
from dcua.keys import is_risky_chord, parse_chord
from test_features import make, run


def test_element_id_cannot_hide_a_consequential_label(tmp_path):
    backend, ctrl = make(tmp_path)

    async def flow():
        snap = await ctrl.snapshot()
        target = next(e.id for e in snap.elements if e.name == "Delete all")
        refused = await ctrl.click(target)
        approved = await ctrl.click(target, confirm=True)
        return refused, approved

    refused, approved = run(ctrl, flow)
    assert refused.status == "needs_confirmation"
    assert approved.ok
    assert len(backend.log) == 1


@pytest.mark.parametrize("mode", ["unknown", "lying"])
def test_foreground_must_be_observed_after_refocus(tmp_path, mode):
    backend, ctrl = make(tmp_path)
    real_risk = ctrl.guard.risk

    async def steal(*args, **kwargs):
        p = await real_risk(*args, **kwargs)
        if mode == "unknown":
            original = backend.list_windows

            def unknown():
                wins = original()
                for win in wins:
                    win.focused = False
                return wins

            backend.list_windows = unknown
        else:
            backend.fg = 2
        backend.focus_window = lambda window: True
        return p

    ctrl.guard.risk = steal
    result = run(ctrl, lambda: ctrl.type_text("must not land elsewhere"))
    assert result.status == "error"
    assert backend.log == [] and backend.editor_text == ""


@pytest.mark.parametrize("action", ["click", "hotkey", "drag"])
def test_revision_is_checked_after_foreground_recovery(tmp_path, action):
    backend, ctrl = make(tmp_path)
    real_focus = backend.focus_window
    real_risk = ctrl.guard.risk
    real_windows = backend.list_windows
    stolen = False

    def windows_after_snapshot():
        wins = real_windows()
        if stolen:
            for win in wins:
                win.focused = win.handle == 2
        return wins

    def refocus_and_edit(window):
        nonlocal stolen
        stolen = False
        ok = real_focus(window)
        backend._win(1).widgets[4].value = "changed during foreground recovery"
        return ok

    async def steal(*args, **kwargs):
        nonlocal stolen
        p = await real_risk(*args, **kwargs)
        # Foreground capture in snapshot can precede the fresh EnumWindows result.
        stolen = True
        return p

    async def flow():
        rev = json.loads(await ctrl.observe(fmt="json"))["revision"]
        ctrl.guard.risk = steal
        backend.list_windows = windows_after_snapshot
        backend.focus_window = refocus_and_edit
        if action == "click":
            return await ctrl.click("the Save button", if_revision=rev)
        if action == "hotkey":
            return await ctrl.hotkey("ctrl+s", if_revision=rev)
        return await ctrl.drag("200,300", "400,500", if_revision=rev)

    result = run(ctrl, flow)
    assert result.status == "stale"
    assert backend.log == ["focus Untitled - Notepad"]


def test_vanished_element_does_not_use_its_old_coordinates(tmp_path):
    backend, ctrl = make(tmp_path)
    backend.current_rect = lambda el: None
    result = run(ctrl, lambda: ctrl.click("the Save button"))
    assert result.status == "error" and backend.log == []


@pytest.mark.parametrize("disabled", [False, True])
def test_missing_verification_never_reports_done(tmp_path, disabled):
    backend, ctrl = make(tmp_path)

    async def unavailable(*args, **kwargs):
        raise DecisionError("offline verification failure")

    ctrl.guard.verify = unavailable
    ctrl.s.verify_actions = not disabled
    result = run(ctrl, lambda: ctrl.type_text("visible text"))
    assert result.ok and backend.editor_text == "visible text"
    assert result.status == "unverified" and result.verified_p is None


@pytest.mark.parametrize("text,submit", [("message", True), ("message\n", False),
                                        ("message\r", False)])
def test_enter_from_typing_always_requires_confirmation(tmp_path, text, submit):
    backend, ctrl = make(tmp_path)
    result = run(ctrl, lambda: ctrl.type_text(text, submit=submit))
    assert result.status == "needs_confirmation" and backend.log == []


@pytest.mark.parametrize("chord", ["f4+alt", "ctrl+alt+f4", "delete+shift",
                                   "ctrl+shift+alt+delete"])
def test_risky_chords_cannot_bypass_gate_by_order_or_extra_modifiers(chord):
    assert is_risky_chord(parse_chord(chord))


def test_targeted_typing_verifies_keyboard_focus_before_clearing(tmp_path):
    backend, ctrl = make(tmp_path)
    backend._fire("open_save")
    backend.focus = None
    backend.mouse_click = lambda *a, **kw: None
    result = run(ctrl, lambda: ctrl.type_text("new.txt", target="File name:", clear=True))
    assert result.status == "error" and backend.log == []


def test_focus_window_does_not_trust_backend_success(tmp_path):
    backend, ctrl = make(tmp_path)
    backend.focus_window = lambda window: True
    result = run(ctrl, lambda: ctrl.focus_window("Downloads"))
    assert result.status == "error" and backend.fg == 1


@pytest.mark.parametrize("clear", [False, True])
def test_typing_checks_the_visible_value_even_if_model_claims_success(tmp_path, clear):
    backend, ctrl = make(tmp_path)
    backend.type_text = lambda text: setattr(backend.focus, "value", "wrong text")
    result = run(ctrl, lambda: ctrl.type_text("expected text", clear=clear))
    assert result.ok and result.status == "unverified"
    assert "typed value" in result.message


def test_malformed_unicode_never_clears_a_field(tmp_path):
    backend, ctrl = make(tmp_path)
    backend.focus.value = "keep this"
    result = run(ctrl, lambda: ctrl.type_text("a" * 40 + "\ud800", clear=True))
    assert result.status == "error" and backend.editor_text == "keep this" and backend.log == []
