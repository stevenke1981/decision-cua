"""EXPERIMENTAL Linux (X11) backend.

Perception via AT-SPI (`python3-pyatspi`, from your distro), input and window
control via `xdotool` / `wmctrl`. Wayland sessions block synthetic input for
most apps; use an X11 session or XWayland apps.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from typing import Any

from ..keys import parse_chord
from .base import Backend, Element, Rect, Snapshot, WindowInfo

XDO_KEYS = {"ctrl": "ctrl", "shift": "shift", "alt": "alt", "win": "super", "enter": "Return",
            "esc": "Escape", "backspace": "BackSpace", "delete": "Delete", "tab": "Tab",
            "space": "space", "pageup": "Prior", "pagedown": "Next", "home": "Home", "end": "End",
            "left": "Left", "right": "Right", "up": "Up", "down": "Down", "insert": "Insert",
            "menu": "Menu", "printscreen": "Print", "capslock": "Caps_Lock"}
INTERACTIVE_STATES = {"push button": "Button", "text": "Edit", "entry": "Edit", "menu item": "MenuItem",
                      "check box": "CheckBox", "radio button": "RadioButton", "combo box": "ComboBox",
                      "link": "Hyperlink", "list item": "ListItem", "page tab": "TabItem",
                      "table cell": "DataItem", "toggle button": "Button", "document web": "Document",
                      "document text": "Document", "label": "Text", "menu": "Menu", "tree item": "TreeItem"}


def _run(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, timeout=5).stdout


class LinuxBackend(Backend):
    name = "linux"

    def __init__(self, click_mode: str = "mouse") -> None:
        if not shutil.which("xdotool"):
            raise RuntimeError("xdotool is required for the Linux backend")
        try:
            import pyatspi  # type: ignore
        except ImportError as exc:
            raise RuntimeError("python3-pyatspi is required (install it from your distro)") from exc
        self.atspi = pyatspi

    def list_windows(self) -> list[WindowInfo]:
        fg = _run("xdotool", "getactivewindow").strip()
        out = []
        if shutil.which("wmctrl"):
            for line in _run("wmctrl", "-lpG").splitlines():
                parts = line.split(None, 8)
                if len(parts) < 9:
                    continue
                wid, _desk, _pid, x, y, w, h, _host, title = parts
                out.append(WindowInfo(handle=int(wid, 16), title=title,
                                      rect=Rect(int(x), int(y), int(x) + int(w), int(y) + int(h)),
                                      focused=fg.isdigit() and int(wid, 16) == int(fg)))
        return out

    def focus_window(self, window: WindowInfo) -> bool:
        _run("xdotool", "windowactivate", "--sync", str(window.handle))
        return True

    def snapshot(self, scope: str = "foreground", max_elements: int = 600,
                 budget_s: float = 4.0) -> Snapshot:
        windows = self.list_windows()
        fg = next((w for w in windows if w.focused), None)
        desktop = self.atspi.Registry.getDesktop(0)
        deadline = time.monotonic() + budget_s
        elements: list[Element] = []
        truncated = False
        for app in desktop:
            if app is None:
                continue
            for frame in app:
                try:
                    title = frame.name or ""
                    active = frame.getState().contains(self.atspi.STATE_ACTIVE)
                except Exception:
                    continue
                if scope != "all" and not active:
                    continue
                truncated |= self._walk(frame, title, elements, max_elements, deadline)
        screen = Rect(0, 0, 10**5, 10**5)
        return Snapshot(windows=windows, foreground=fg, elements=elements, screen=screen,
                        truncated=truncated)

    def _walk(self, node: Any, title: str, out: list[Element], limit: int, deadline: float) -> bool:
        stack = [(node, 0)]
        while stack:
            if len(out) >= limit or time.monotonic() > deadline:
                return True
            n, depth = stack.pop()
            try:
                role_name = n.getRoleName()
                name = (n.name or "").strip()
                states = n.getState()
                showing = states.contains(self.atspi.STATE_SHOWING)
                ext = n.queryComponent().getExtents(self.atspi.DESKTOP_COORDS)
                rect = Rect(ext.x, ext.y, ext.x + ext.width, ext.y + ext.height)
            except Exception:
                continue
            role = INTERACTIVE_STATES.get(role_name)
            if depth > 0 and showing and rect.area > 1 and (name or role in {"Edit", "Document"}):
                value = None
                if role == "Edit":
                    try:
                        t = n.queryText()
                        value = t.getText(0, min(t.characterCount, 200))
                    except Exception:
                        pass
                out.append(Element(id="", role=role or role_name.title().replace(" ", ""), name=name,
                                   value=value, rect=rect, window=title, depth=depth,
                                   enabled=states.contains(self.atspi.STATE_ENABLED),
                                   focused=states.contains(self.atspi.STATE_FOCUSED), handle=n))
            if depth < 30:
                try:
                    kids = [n.getChildAtIndex(i) for i in range(n.childCount)]
                except Exception:
                    kids = []
                stack.extend((k, depth + 1) for k in reversed(kids) if k is not None)
        return False

    def current_rect(self, el: Element) -> Rect | None:
        try:
            ext = el.handle.queryComponent().getExtents(self.atspi.DESKTOP_COORDS)
            return Rect(ext.x, ext.y, ext.x + ext.width, ext.y + ext.height)
        except Exception:
            return el.rect

    def mouse_click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        btn = {"left": "1", "middle": "2", "right": "3"}[button]
        _run("xdotool", "mousemove", str(x), str(y), "click", "--repeat", "2" if double else "1", btn)

    def mouse_drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left",
                   steps: int = 20) -> None:
        btn = {"left": "1", "middle": "2", "right": "3"}[button]
        # xdotool would read a negative coordinate as an option; X screens start at 0.
        x1, y1, x2, y2 = (max(0, int(v)) for v in (x1, y1, x2, y2))
        _run("xdotool", "mousemove", str(x1), str(y1), "mousedown", btn)
        try:
            n = max(1, steps)
            for i in range(1, n + 1):
                _run("xdotool", "mousemove", str(x1 + (x2 - x1) * i // n), str(y1 + (y2 - y1) * i // n))
        finally:
            _run("xdotool", "mouseup", btn)  # never leave the button held down

    def type_text(self, text: str) -> None:
        _run("xdotool", "type", "--delay", "8", "--", text)

    def press(self, keys: list[str]) -> None:
        keys = parse_chord(keys)
        _run("xdotool", "key", "+".join(XDO_KEYS.get(k, k) for k in keys))

    def scroll(self, x: int, y: int, clicks: int) -> None:
        btn = "4" if clicks > 0 else "5"
        _run("xdotool", "mousemove", str(x), str(y), "click", "--repeat", str(abs(clicks)), btn)

    def launch(self, target: str) -> bool:
        try:
            subprocess.Popen(["xdg-open", target] if "/" in target or ":" in target else [target])
            return True
        except OSError:
            return False
