"""In-memory desktop for tests and dry-run demos (`DCUA_BACKEND=fake`).

Scenario: a Notepad-like editor. Typing goes to the focused edit; clicking
"Save" opens a "Save As" dialog; typing a file name and pressing the dialog's
Save button closes it and renames the window. Ctrl+S also opens the dialog.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .base import Backend, Element, Rect, Snapshot, WindowInfo

SCREEN = Rect(0, 0, 1920, 1080)


@dataclass
class FakeWidget:
    role: str
    name: str
    rect: Rect
    automation_id: str = ""
    value: str | None = None
    on_click: str = ""          # action key
    path: str = ""


@dataclass
class FakeWindow:
    handle: int
    title: str
    process: str
    rect: Rect
    widgets: list[FakeWidget] = field(default_factory=list)


class FakeBackend(Backend):
    name = "fake"

    def __init__(self, extra_buttons: int = 0) -> None:
        editor = FakeWindow(1, "Untitled - Notepad", "notepad.exe", Rect(100, 50, 1300, 850))
        editor.widgets = [
            FakeWidget("MenuItem", "File", Rect(110, 80, 150, 100), path="MenuBar 'Application'"),
            FakeWidget("MenuItem", "Edit", Rect(155, 80, 195, 100), path="MenuBar 'Application'"),
            FakeWidget("Button", "Save", Rect(210, 80, 260, 100), "SaveButton", on_click="open_save"),
            FakeWidget("Button", "Delete all", Rect(270, 80, 350, 100), "ClearButton", on_click="clear"),
            FakeWidget("Document", "Text editor", Rect(110, 110, 1290, 830), "RichEditD2DPT", value=""),
        ]
        for i in range(extra_buttons):  # padding to exercise multi-round grounding
            editor.widgets.append(FakeWidget("Button", f"Tool {i}", Rect(400 + i * 4, 80, 403 + i * 4, 100)))
        self.windows: list[FakeWindow] = [
            editor,
            FakeWindow(2, "Downloads - File Explorer", "explorer.exe", Rect(200, 200, 1000, 700),
                       [FakeWidget("ListItem", "report.pdf", Rect(220, 260, 500, 280))]),
        ]
        self.fg = editor.handle
        self.focus: FakeWidget | None = editor.widgets[4]
        self.log: list[str] = []
        self.saved_as: str | None = None

    # ---------------------------------------------------------------- helpers
    def _win(self, handle: int) -> FakeWindow:
        return next(w for w in self.windows if w.handle == handle)

    @property
    def editor_text(self) -> str:
        return self._win(1).widgets[4].value or ""

    def _info(self, w: FakeWindow) -> WindowInfo:
        return WindowInfo(handle=w.handle, title=w.title, process=w.process, rect=w.rect,
                          focused=w.handle == self.fg)

    # ---------------------------------------------------------------- Backend
    def list_windows(self) -> list[WindowInfo]:
        return [self._info(w) for w in self.windows]

    def focus_window(self, window: WindowInfo) -> bool:
        self.fg = int(window.handle)
        self.log.append(f"focus {window.title}")
        return True

    def snapshot(self, scope: str = "foreground", max_elements: int = 600,
                 budget_s: float = 4.0) -> Snapshot:
        wins = self.windows if scope == "all" else [self._win(self.fg)]
        elements = [
            Element(id="", role=wd.role, name=wd.name, automation_id=wd.automation_id,
                    value=wd.value, rect=wd.rect, focused=wd is self.focus, window=w.title,
                    path=wd.path, depth=2, handle=wd)
            for w in wins for wd in w.widgets
        ][:max_elements]
        return Snapshot(windows=self.list_windows(), foreground=self._info(self._win(self.fg)),
                        elements=elements, screen=SCREEN)

    def current_rect(self, el: Element) -> Rect | None:
        return el.handle.rect if isinstance(el.handle, FakeWidget) else el.rect

    def _hit(self, x: int, y: int) -> FakeWidget | None:
        for wd in reversed(self._win(self.fg).widgets):
            if wd.rect.contains(x, y):
                return wd
        return None

    def mouse_click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        wd = self._hit(x, y)
        self.log.append(f"click {button} ({x},{y}) -> {wd.name if wd else None}")
        if wd is None:
            return
        if wd.role in {"Edit", "Document"}:
            self.focus = wd
        self._fire(wd.on_click)

    def mouse_drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left",
                   steps: int = 20) -> None:
        self.log.append(f"drag {button} ({x1},{y1}) -> ({x2},{y2})")

    def _fire(self, action: str) -> None:
        if action == "open_save":
            dlg = FakeWindow(3, "Save As", "notepad.exe", Rect(400, 300, 1000, 600))
            name_box = FakeWidget("Edit", "File name:", Rect(500, 450, 900, 475), "1001", value="*.txt",
                                  path="Pane 'Save As'")
            dlg.widgets = [
                name_box,
                FakeWidget("Button", "Save", Rect(780, 540, 860, 565), "1", on_click="do_save"),
                FakeWidget("Button", "Cancel", Rect(870, 540, 950, 565), "2", on_click="cancel"),
            ]
            self.windows.append(dlg)
            self.fg = 3
            self.focus = name_box
        elif action == "do_save":
            dlg = self._win(3)
            self.saved_as = dlg.widgets[0].value or ""
            self.windows.remove(dlg)
            self.fg = 1
            self._win(1).title = f"{self.saved_as} - Notepad"
            self.focus = self._win(1).widgets[4]
        elif action == "cancel":
            self.windows = [w for w in self.windows if w.handle != 3]
            self.fg = 1
        elif action == "clear":
            self._win(1).widgets[4].value = ""

    def invoke(self, el: Element) -> bool:
        if isinstance(el.handle, FakeWidget) and el.handle.on_click:
            self._fire(el.handle.on_click)
            return True
        return False

    def set_value(self, el: Element, text: str) -> bool:
        if isinstance(el.handle, FakeWidget) and el.handle.value is not None:
            el.handle.value = text
            return True
        return False

    def type_text(self, text: str) -> None:
        self.log.append(f"type {text!r}")
        if self.focus is not None and self.focus.value is not None:
            self.focus.value = (self.focus.value or "") + text

    def press(self, keys: list[str]) -> None:
        self.log.append("press " + "+".join(keys))
        if keys == ["ctrl", "s"] and self.fg == 1:
            self._fire("open_save")
        elif keys == ["enter"] and self.fg == 3:
            self._fire("do_save")
        elif keys == ["ctrl", "a"]:
            self._select_all = True
        elif keys in (["delete"], ["backspace"]) and getattr(self, "_select_all", False):
            if self.focus is not None and self.focus.value is not None:
                self.focus.value = ""
            self._select_all = False

    def scroll(self, x: int, y: int, clicks: int) -> None:
        self.log.append(f"scroll {clicks} at ({x},{y})")

    def launch(self, target: str) -> bool:
        self.log.append(f"launch {target}")
        return True
