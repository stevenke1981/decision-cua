"""press() must close the focused IME while the chord is sent (regression: an open CJK IME
turned "x" into composition text and swallowed keys)."""
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows SendInput backend")


def test_press_sends_chord_inside_ime_closed_block(monkeypatch, windows_module):
    w = windows_module

    events: list[str] = []

    class FakeIME:
        def __enter__(self):
            events.append("ime-closed")

        def __exit__(self, *exc):
            events.append("ime-restored")

    def fake_send(*inputs):
        events.append(f"send:{len(inputs)}")

    monkeypatch.setattr(w, "_ime_closed", lambda: FakeIME())
    monkeypatch.setattr(w, "_send", fake_send)
    monkeypatch.setattr(w.time, "sleep", lambda _s: None)

    object.__new__(w.WindowsBackend).press(["ctrl", "l"])

    assert events == ["ime-closed", "send:2", "send:2", "ime-restored"]
