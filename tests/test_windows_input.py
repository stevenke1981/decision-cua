"""Exercise real event construction with all OS/IME entry points replaced by doubles."""
from contextlib import nullcontext

import pytest


@pytest.fixture
def input_double(monkeypatch, windows_module):
    w = windows_module
    events = []

    def capture(*inputs):
        events.extend((i.u.ki.wVk, i.u.ki.wScan, i.u.ki.dwFlags) for i in inputs)

    monkeypatch.setattr(w, "_send", capture)
    monkeypatch.setattr(w, "_ime_closed", nullcontext)
    monkeypatch.setattr(w.time, "sleep", lambda seconds: None)
    return w, object.__new__(w.WindowsBackend), events


@pytest.mark.parametrize("text", ["你好，世界。", "A😀𠮷", "a" * 31 + "😀b"])
def test_unicode_units_and_keyups_survive_batch_boundaries(input_double, text):
    w, backend, events = input_double
    backend.type_text(text)
    raw = text.encode("utf-16-le")
    units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
    expected = []
    for unit in units:
        expected.extend([(0, unit, w.KEYEVENTF_UNICODE),
                         (0, unit, w.KEYEVENTF_UNICODE | w.KEYEVENTF_KEYUP)])
    assert events == expected


def test_crlf_and_lone_cr_are_enter_events(input_double):
    w, backend, events = input_double
    backend.type_text("a\r\nb\rc\t")
    assert sum(vk == w.VK["enter"] and flags == 0 for vk, _, flags in events) == 2
    assert sum(vk == w.VK["tab"] and flags == 0 for vk, _, flags in events) == 1


def test_invalid_unicode_is_rejected_before_any_input(input_double):
    _, backend, events = input_double
    with pytest.raises(UnicodeEncodeError):
        backend.type_text("a" * 40 + "\ud800")
    assert events == []


def test_press_releases_keys_when_send_fails(input_double, monkeypatch):
    w, backend, _ = input_double
    calls = []

    def partial_send(*inputs):
        calls.append([i.u.ki.dwFlags for i in inputs])
        if len(calls) == 1:
            raise OSError("partial SendInput")

    monkeypatch.setattr(w, "_send", partial_send)
    with pytest.raises(OSError, match="partial"):
        backend.press(["ctrl", "l"])
    assert calls == [[0, 0], [w.KEYEVENTF_KEYUP, w.KEYEVENTF_KEYUP]]


@pytest.mark.parametrize("action", ["click", "drag", "scroll"])
def test_cursor_position_failure_aborts_before_send(input_double, monkeypatch, action):
    w, backend, events = input_double
    monkeypatch.setattr(w.user32, "SetCursorPos", lambda x, y: 0)
    monkeypatch.setattr(w, "virtual_screen", lambda: w.Rect(0, 0, 100, 100))
    with pytest.raises(OSError, match="SetCursorPos"):
        if action == "click":
            backend.mouse_click(10, 20)
        elif action == "drag":
            backend.mouse_drag(10, 20, 30, 40)
        else:
            backend.scroll(10, 20, 1)
    assert events == []
