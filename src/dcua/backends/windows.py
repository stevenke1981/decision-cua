"""Windows backend.

Perception: UI Automation tree via the `uiautomation` package (COM wrapper).
Input:      raw SendInput through ctypes (Unicode typing works for CJK, no extra deps).
Windows:    EnumWindows through ctypes (fast, filters cloaked/tool windows).

Notes
- The process is made Per-Monitor-DPI-aware before anything else, so UIA rects
  and SetCursorPos share the same physical-pixel coordinate space.
- Windows UIPI blocks input into elevated (admin) windows from a non-elevated
  process. Run dcua elevated if you need to drive admin apps.
- Every method must run on the thread that created the backend (COM).
"""
from __future__ import annotations

import ctypes
import io
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from ctypes import wintypes
from typing import Any

from ..keys import EXTENDED, VK, parse_chord
from .base import Backend, Element, Rect, Snapshot, WindowInfo

if sys.platform != "win32":  # pragma: no cover - import guard
    raise ImportError("WindowsBackend is only available on Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")


# ----------------------------------------------------------------------------- DPI
def enable_dpi_awareness() -> None:
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


enable_dpi_awareness()

# ----------------------------------------------------------------------------- SendInput
ULONG_PTR = ctypes.c_size_t
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
MOUSE_FLAGS = {
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_MOVE, MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x4000, 0x8000
WHEEL_DELTA = 120


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


def _send(*inputs: INPUT) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        err = ctypes.get_last_error()
        raise OSError(
            f"SendInput injected {sent}/{len(inputs)} events (error {err}). "
            "If the target window runs as administrator, run dcua elevated too."
        )


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _mouse(flags: int, data: int = 0, dx: int = 0, dy: int = 0) -> INPUT:
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _move_to(x: int, y: int) -> INPUT:
    """Absolute move as real input (apps that track WM_MOUSEMOVE during a drag need this)."""
    vs = virtual_screen()
    w, h = max(1, vs.width), max(1, vs.height)
    x = min(max(int(x), vs.left), vs.left + w - 1)
    y = min(max(int(y), vs.top), vs.top + h - 1)
    # Windows maps n -> pixel n * width / 65536 (truncated); this inverse lands on the exact pixel.
    nx = ((x - vs.left) * 65536 + w - 1) // w
    ny = ((y - vs.top) * 65536 + h - 1) // h
    return _mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, dx=nx, dy=ny)


# ----------------------------------------------------------------------------- IME
# An open CJK IME (Bopomofo, Pinyin, ...) lets VK_PACKET ideographs through but swallows
# full-width punctuation into its composition buffer, so "你好，世界。" lands as
# "你好世界" + "，。" later. Closing the IME of the focused window while typing avoids it;
# WM_IME_CONTROL on the default IME window works across processes.
imm32 = ctypes.WinDLL("imm32")
imm32.ImmGetDefaultIMEWnd.argtypes = [wintypes.HWND]
imm32.ImmGetDefaultIMEWnd.restype = wintypes.HWND
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
WM_IME_CONTROL, IMC_GETOPENSTATUS, IMC_SETOPENSTATUS = 0x0283, 0x0005, 0x0006


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND), ("hwndMoveSize", wintypes.HWND),
                ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]


def _focus_hwnd() -> int:
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if user32.GetGUIThreadInfo(0, ctypes.byref(info)) and info.hwndFocus:
        return info.hwndFocus
    return user32.GetForegroundWindow() or 0


@contextmanager
def _ime_closed():
    """Temporarily close the focused window's IME; restore it afterwards."""
    hwnd = _focus_hwnd()
    ime = imm32.ImmGetDefaultIMEWnd(hwnd) if hwnd else None
    was_open = bool(ime) and user32.SendMessageW(ime, WM_IME_CONTROL, IMC_GETOPENSTATUS, 0) != 0
    if was_open:
        user32.SendMessageW(ime, WM_IME_CONTROL, IMC_SETOPENSTATUS, 0)
        time.sleep(0.03)
    try:
        yield
    finally:
        if was_open:
            time.sleep(0.03)
            user32.SendMessageW(ime, WM_IME_CONTROL, IMC_SETOPENSTATUS, 1)


def _vk_for(key: str) -> tuple[int, int]:
    """Return (vk, extra_flags) for a parsed key name."""
    if key in VK:
        return VK[key], (KEYEVENTF_EXTENDEDKEY if key in EXTENDED else 0)
    if len(key) == 1:
        ch = key
        if ch.isalnum() and ch.isascii():
            return ord(ch.upper()), 0
        res = user32.VkKeyScanW(ch)
        if res == -1:
            raise ValueError(f"no virtual key for {ch!r} on this keyboard layout")
        return res & 0xFF, 0
    raise ValueError(f"unknown key {key!r}")


# ----------------------------------------------------------------------------- window helpers
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_CLOAKED = 14
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _sig(fn: Any, argtypes: list[Any], restype: Any) -> None:
    fn.argtypes = argtypes
    fn.restype = restype


HWND, DWORD, BOOL = wintypes.HWND, wintypes.DWORD, wintypes.BOOL
_sig(user32.GetForegroundWindow, [], HWND)
_sig(user32.GetWindowTextLengthW, [HWND], ctypes.c_int)
_sig(user32.GetWindowTextW, [HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
_sig(user32.IsWindowVisible, [HWND], BOOL)
_sig(user32.IsIconic, [HWND], BOOL)
_sig(user32.GetWindowLongW, [HWND, ctypes.c_int], ctypes.c_long)
_sig(user32.GetWindowRect, [HWND, ctypes.POINTER(wintypes.RECT)], BOOL)
_sig(user32.ShowWindow, [HWND, ctypes.c_int], BOOL)
_sig(user32.BringWindowToTop, [HWND], BOOL)
_sig(user32.SetForegroundWindow, [HWND], BOOL)
_sig(user32.GetWindowThreadProcessId, [HWND, ctypes.POINTER(DWORD)], DWORD)
_sig(user32.AttachThreadInput, [DWORD, DWORD, BOOL], BOOL)
_sig(user32.SetCursorPos, [ctypes.c_int, ctypes.c_int], BOOL)
_sig(user32.VkKeyScanW, [wintypes.WCHAR], ctypes.c_short)
_sig(user32.GetSystemMetrics, [ctypes.c_int], ctypes.c_int)
_sig(user32.EnumWindows, [EnumWindowsProc, wintypes.LPARAM], BOOL)
_sig(kernel32.OpenProcess, [DWORD, BOOL, DWORD], wintypes.HANDLE)
_sig(kernel32.CloseHandle, [wintypes.HANDLE], BOOL)
_sig(kernel32.GetCurrentThreadId, [], DWORD)
_sig(kernel32.QueryFullProcessImageNameW,
     [wintypes.HANDLE, DWORD, wintypes.LPWSTR, ctypes.POINTER(DWORD)], BOOL)
_sig(user32.GetClassNameW, [HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
_sig(user32.GetWindow, [HWND, wintypes.UINT], HWND)
_sig(dwmapi.DwmGetWindowAttribute, [HWND, DWORD, ctypes.c_void_p, DWORD], ctypes.c_long)


def _window_text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


# Win32 menus, combo drop-downs, tooltips-with-content and XAML/Chromium popups are
# separate top-level windows, not children of the app window in the UIA tree.
POPUP_CLASSES = {"#32768", "ComboLBox", "DropDown", "Xaml_WindowedPopupClass",
                 "Chrome_WidgetWin_2", "SysListView32", "Net UI Tool Window"}
GW_OWNER = 4


def _popups_for(fg_hwnd: int) -> list[int]:
    """Visible popup windows that belong to the foreground app (menus, drop-downs)."""
    if not fg_hwnd:
        return []
    fg_pid = _pid(fg_hwnd)
    found: list[int] = []

    def cb(hwnd: int, _lp: int) -> bool:
        if hwnd == fg_hwnd or not user32.IsWindowVisible(hwnd) or _is_cloaked(hwnd):
            return True
        r = _window_rect(hwnd)
        if r.area <= 16:
            return True
        cls = _class_name(hwnd)
        owner = user32.GetWindow(hwnd, GW_OWNER)
        if cls == "#32768" or (cls in POPUP_CLASSES and _pid(hwnd) == fg_pid) or \
                (owner == fg_hwnd and not _window_text(hwnd)):
            found.append(int(hwnd))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found


def _is_cloaked(hwnd: int) -> bool:
    cloaked = wintypes.DWORD(0)
    try:
        dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED,
                                     ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    except OSError:
        return False
    return cloaked.value != 0


def _process_name(hwnd: int) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def _window_rect(hwnd: int) -> Rect:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return Rect(r.left, r.top, r.right, r.bottom)


def virtual_screen() -> Rect:
    x, y = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    w, h = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    return Rect(x, y, x + w, y + h)


# ----------------------------------------------------------------------------- backend
KEEP_UNNAMED_ROLES = {"Edit", "Document", "ComboBox", "CheckBox", "RadioButton", "Slider",
                      "Spinner", "ScrollBar", "List", "Tree", "DataGrid", "Table", "Tab"}
SKIP_ROLES = {"TitleBar", "Thumb", "Separator"}


class WindowsBackend(Backend):
    name = "windows"

    def __init__(self, click_mode: str = "mouse") -> None:
        import uiautomation as auto  # imported here: needs Windows + COM

        self.auto = auto
        self.click_mode = click_mode
        self._com = auto.UIAutomationInitializerInThread()
        auto.SetGlobalSearchTimeout(1.0)
        self._value_pattern = auto.PatternId.ValuePattern
        self._invoke_pattern = auto.PatternId.InvokePattern

    def close(self) -> None:
        try:
            del self._com
        except AttributeError:
            pass

    # ------------------------------------------------------------- windows
    def list_windows(self) -> list[WindowInfo]:
        fg = user32.GetForegroundWindow()
        out: list[WindowInfo] = []

        def cb(hwnd: int, _lp: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            title = _window_text(hwnd)
            if not title or _is_cloaked(hwnd):
                return True
            if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                return True
            out.append(WindowInfo(
                handle=int(hwnd), title=title, process=_process_name(hwnd),
                rect=_window_rect(hwnd), focused=(hwnd == fg),
                minimized=bool(user32.IsIconic(hwnd)),
            ))
            return True

        user32.EnumWindows(EnumWindowsProc(cb), 0)
        return out

    def focus_window(self, window: WindowInfo) -> bool:
        hwnd = int(window.handle)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        fg = user32.GetForegroundWindow()
        cur_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None)
        attached = False
        if fg_tid and fg_tid != cur_tid:
            attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
        try:
            user32.BringWindowToTop(hwnd)
            ok = bool(user32.SetForegroundWindow(hwnd))
        finally:
            if attached:
                user32.AttachThreadInput(cur_tid, fg_tid, False)
        if not ok:
            # Fallback: holding ALT during the call grants foreground rights.
            _send(_key(VK["alt"]))
            try:
                ok = bool(user32.SetForegroundWindow(hwnd))
            finally:
                _send(_key(VK["alt"], flags=KEYEVENTF_KEYUP))
        time.sleep(0.15)
        return ok and user32.GetForegroundWindow() == hwnd

    # ------------------------------------------------------------- perception
    def snapshot(self, scope: str = "foreground", max_elements: int = 600,
                 budget_s: float = 4.0) -> Snapshot:
        windows = self.list_windows()
        fg_hwnd = user32.GetForegroundWindow()
        foreground = next((w for w in windows if w.handle == fg_hwnd), None)
        if foreground is None and fg_hwnd:
            foreground = WindowInfo(handle=int(fg_hwnd), title=_window_text(fg_hwnd),
                                    rect=_window_rect(fg_hwnd), focused=True)
        targets: list[WindowInfo]
        if scope == "all":
            targets = [w for w in windows if not w.minimized]
            if foreground in targets:  # foreground first so it wins the element budget
                targets.remove(foreground)
                targets.insert(0, foreground)
        else:
            targets = [foreground] if foreground else []
            if foreground:
                for h in _popups_for(int(foreground.handle)):
                    targets.append(WindowInfo(handle=h, title=f"{foreground.title} (popup)",
                                              rect=_window_rect(h), focused=False))

        screen = virtual_screen()
        deadline = time.monotonic() + budget_s
        elements: list[Element] = []
        truncated = False
        for w in targets:
            try:
                root = self.auto.ControlFromHandle(int(w.handle))
            except Exception:
                continue
            if root is None:
                continue
            got, cut = self._walk(root, w.title, screen, max_elements - len(elements), deadline)
            elements.extend(got)
            truncated |= cut
            if len(elements) >= max_elements or time.monotonic() > deadline:
                truncated = True
                break
        return Snapshot(windows=windows, foreground=foreground, elements=elements,
                        screen=screen, truncated=truncated)

    def _walk(self, root: Any, window_title: str, screen: Rect, limit: int,
              deadline: float) -> tuple[list[Element], bool]:
        out: list[Element] = []
        stack: list[tuple[Any, int, tuple[str, ...]]] = [(root, 0, ())]
        max_depth = 30
        while stack:
            if len(out) >= limit or time.monotonic() > deadline:
                return out, True
            ctrl, depth, crumbs = stack.pop()
            try:
                role = (ctrl.ControlTypeName or "").removesuffix("Control")
                name = (ctrl.Name or "").strip()
                br = ctrl.BoundingRectangle
                rect = Rect(br.left, br.top, br.right, br.bottom)
                offscreen = bool(ctrl.IsOffscreen)
            except Exception:
                continue

            visible = rect.width > 1 and rect.height > 1 and not offscreen and rect.intersects(screen)
            if depth > 0 and visible and role not in SKIP_ROLES:
                try:
                    auto_id = ctrl.AutomationId or ""
                except Exception:
                    auto_id = ""
                if name or auto_id or role in KEEP_UNNAMED_ROLES:
                    out.append(self._element(ctrl, role, name, auto_id, rect, window_title, crumbs, depth))

            if depth < max_depth and (visible or depth == 0 or rect.area == 0):
                try:
                    children = ctrl.GetChildren()
                except Exception:
                    children = []
                child_crumbs = crumbs
                if depth > 0 and name and role in {"Pane", "Group", "ToolBar", "MenuBar", "Menu",
                                                    "Tab", "TabItem", "List", "Tree", "Window",
                                                    "Document", "Custom", "DataGrid", "Table"}:
                    child_crumbs = (crumbs + (f"{role} '{name[:30]}'",))[-2:]
                for c in reversed(children):
                    stack.append((c, depth + 1, child_crumbs))
        return out, False

    def _element(self, ctrl: Any, role: str, name: str, auto_id: str, rect: Rect,
                 window_title: str, crumbs: tuple[str, ...], depth: int) -> Element:
        try:
            enabled = bool(ctrl.IsEnabled)
        except Exception:
            enabled = True
        try:
            focused = bool(ctrl.HasKeyboardFocus)
        except Exception:
            focused = False
        try:
            class_name = ctrl.ClassName or ""
        except Exception:
            class_name = ""
        value = None
        if role in {"Edit", "ComboBox", "Document", "Spinner"}:
            value = self._read_value(ctrl)
        return Element(id="", role=role, name=name, automation_id=auto_id, class_name=class_name,
                       value=value, rect=rect, enabled=enabled, focused=focused,
                       window=window_title, path=" > ".join(crumbs), depth=depth, handle=ctrl)

    def _read_value(self, ctrl: Any) -> str | None:
        try:
            if getattr(ctrl, "IsPassword", False):
                return "••••"
            pat = ctrl.GetPattern(self._value_pattern)
            if pat is None:
                return None
            v = pat.Value
            if v is None:
                return None
            v = str(v).replace("\r", " ").replace("\n", " ")
            return v[:200]
        except Exception:
            return None

    def current_rect(self, el: Element) -> Rect | None:
        if el.handle is None:
            return el.rect
        try:
            br = el.handle.BoundingRectangle
            r = Rect(br.left, br.top, br.right, br.bottom)
            return r if r.area > 0 else None
        except Exception:
            return None

    # ------------------------------------------------------------- input
    def mouse_click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        down, up = MOUSE_FLAGS[button]
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.03)
        for _ in range(2 if double else 1):
            _send(_mouse(down), _mouse(up))
            time.sleep(0.04)

    def mouse_drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left",
                   steps: int = 20) -> None:
        down, up = MOUSE_FLAGS[button]
        n = max(1, steps)
        user32.SetCursorPos(int(x1), int(y1))
        _send(_move_to(x1, y1))
        time.sleep(0.03)
        _send(_mouse(down))
        try:
            time.sleep(0.08)  # let the app register the press before motion (drag-drop thresholds)
            for i in range(1, n + 1):
                time.sleep(0.01)
                _send(_move_to(x1 + (x2 - x1) * i // n, y1 + (y2 - y1) * i // n))
            time.sleep(0.03)
        finally:
            _send(_mouse(up))  # never leave a button held down

    def invoke(self, el: Element) -> bool:
        if el.handle is None:
            return False
        try:
            pat = el.handle.GetPattern(self._invoke_pattern)
            if pat is None:
                return False
            pat.Invoke()
            return True
        except Exception:
            return False

    def set_value(self, el: Element, text: str) -> bool:
        if el.handle is None:
            return False
        try:
            pat = el.handle.GetPattern(self._value_pattern)
            if pat is None or getattr(pat, "IsReadOnly", False):
                return False
            pat.SetValue(text)
            return True
        except Exception:
            return False

    def type_text(self, text: str) -> None:
        batch: list[INPUT] = []

        def flush() -> None:
            if batch:
                _send(*batch)
                batch.clear()
                time.sleep(0.01)

        with _ime_closed():
            for ch in text:
                if ch == "\r":
                    continue
                if ch in "\n\t":
                    vk = VK["enter"] if ch == "\n" else VK["tab"]
                    batch += [_key(vk), _key(vk, flags=KEYEVENTF_KEYUP)]
                else:
                    units = ch.encode("utf-16-le")
                    for i in range(0, len(units), 2):  # surrogate pairs -> two units
                        unit = int.from_bytes(units[i:i + 2], "little")
                        batch += [_key(0, unit, KEYEVENTF_UNICODE),
                                  _key(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
                if len(batch) >= 64:
                    flush()
            flush()

    def press(self, keys: list[str]) -> None:
        keys = parse_chord(keys)
        codes = [_vk_for(k) for k in keys]
        downs = [_key(vk, flags=fl) for vk, fl in codes]
        ups = [_key(vk, flags=fl | KEYEVENTF_KEYUP) for vk, fl in reversed(codes)]
        # An open CJK IME turns letter keys into composition (e.g. "x" -> 力), so close it
        # for the duration of the chord, the same way type_text does.
        with _ime_closed():
            _send(*downs)
            time.sleep(0.03)
            _send(*ups)

    def scroll(self, x: int, y: int, clicks: int) -> None:
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.03)
        _send(_mouse(MOUSEEVENTF_WHEEL, clicks * WHEEL_DELTA))

    # ------------------------------------------------------------- misc
    def launch(self, target: str) -> bool:
        try:
            os.startfile(target)  # type: ignore[attr-defined]  # app name, path, or URI
            return True
        except OSError:
            try:
                subprocess.Popen(["cmd", "/c", "start", "", target], shell=False,
                                 creationflags=0x08000000)  # CREATE_NO_WINDOW
                return True
            except OSError:
                return False

    def screenshot(self) -> tuple[bytes, Rect] | None:
        try:
            from PIL import ImageGrab
        except ImportError:
            return None
        img = ImageGrab.grab(all_screens=True)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), virtual_screen()
