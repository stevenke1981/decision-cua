"""Platform-neutral data model and backend interface.

Backends are synchronous and are always driven from ONE dedicated thread
(see executor.BackendThread), because Windows UI Automation is COM and
must be initialised and used on the same thread.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def intersects(self, other: "Rect") -> bool:
        return not (
            other.left >= self.right
            or other.right <= self.left
            or other.top >= self.bottom
            or other.bottom <= self.top
        )


@dataclass
class Element:
    id: str                      # stable id assigned by IdRegistry, e.g. "e12"
    role: str                    # Button, Edit, MenuItem, ListItem, ...
    name: str
    automation_id: str = ""
    class_name: str = ""
    value: str | None = None     # text value for edits/combos (masked for passwords)
    rect: Rect | None = None
    enabled: bool = True
    focused: bool = False
    window: str = ""             # title of the top-level window it belongs to
    path: str = ""               # short breadcrumb of named ancestors
    depth: int = 0
    handle: Any = field(default=None, repr=False, compare=False)

    def signature(self) -> tuple[str, ...]:
        return (self.window, self.role, self.name, self.automation_id, self.path)

    def describe(self, *, position: bool = True, window: bool = True) -> str:
        label = self.name or self.automation_id or self.class_name or "(unnamed)"
        parts = [f'{self.role} "{label}"']
        if self.automation_id and self.automation_id != label:
            parts.append(f"id={self.automation_id}")
        if self.value:
            v = self.value if len(self.value) <= 60 else self.value[:57] + "..."
            parts.append(f'value="{v}"')
        if self.path:
            parts.append(f"in {self.path}")
        if window and self.window:
            parts.append(f'window "{self.window}"')
        if position and self.rect:
            x, y = self.rect.center
            parts.append(f"at ({x},{y})")
        if not self.enabled:
            parts.append("disabled")
        if self.focused:
            parts.append("focused")
        return ", ".join(parts)


@dataclass
class WindowInfo:
    handle: Any
    title: str
    process: str = ""
    rect: Rect | None = None
    focused: bool = False
    minimized: bool = False

    def describe(self) -> str:
        bits = [f'"{self.title}"']
        if self.process:
            bits.append(self.process)
        if self.focused:
            bits.append("foreground")
        if self.minimized:
            bits.append("minimized")
        return " | ".join(bits)


@dataclass
class Snapshot:
    windows: list[WindowInfo]
    foreground: WindowInfo | None
    elements: list[Element]
    screen: Rect
    taken_at: float = field(default_factory=time.time)
    truncated: bool = False

    def by_id(self, eid: str) -> Element | None:
        for el in self.elements:
            if el.id == eid:
                return el
        return None

    @property
    def focused(self) -> Element | None:
        for el in self.elements:
            if el.focused:
                return el
        return None


class Backend(ABC):
    """Synchronous OS automation backend."""

    name = "base"

    @abstractmethod
    def list_windows(self) -> list[WindowInfo]: ...

    @abstractmethod
    def focus_window(self, window: WindowInfo) -> bool: ...

    @abstractmethod
    def snapshot(self, scope: str = "foreground", max_elements: int = 600,
                 budget_s: float = 4.0) -> Snapshot:
        """scope: 'foreground' (default) or 'all' (every top-level window, expensive)."""

    @abstractmethod
    def current_rect(self, el: Element) -> Rect | None:
        """Re-read an element's live bounding box (it may have moved)."""

    @abstractmethod
    def mouse_click(self, x: int, y: int, button: str = "left", double: bool = False) -> None: ...

    @abstractmethod
    def type_text(self, text: str) -> None: ...

    @abstractmethod
    def press(self, keys: list[str]) -> None:
        """Press a chord, e.g. ['ctrl', 's']."""

    @abstractmethod
    def scroll(self, x: int, y: int, clicks: int) -> None:
        """clicks > 0 scrolls up, < 0 scrolls down."""

    # ------------------------------------------------------------- optional
    def invoke(self, el: Element) -> bool:
        """Accessibility 'invoke' instead of a mouse click. Return False if unsupported."""
        return False

    def set_value(self, el: Element, text: str) -> bool:
        """Set an edit's value directly. Return False if unsupported."""
        return False

    def launch(self, target: str) -> bool:
        return False

    def screenshot(self) -> tuple[bytes, Rect] | None:
        """PNG bytes plus the virtual-screen rect the image covers."""
        return None

    def close(self) -> None:
        pass
