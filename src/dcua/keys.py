"""Key-chord parsing shared by backends (pure Python, testable anywhere)."""
from __future__ import annotations

ALIASES = {
    "control": "ctrl", "ctl": "ctrl", "cmd": "win", "super": "win", "meta": "win",
    "windows": "win", "option": "alt", "return": "enter", "escape": "esc",
    "del": "delete", "ins": "insert", "pgup": "pageup", "pgdn": "pagedown",
    "page_up": "pageup", "page_down": "pagedown", "bksp": "backspace", "spacebar": "space",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
    "apps": "menu", "context": "menu", "prtsc": "printscreen",
}
MODIFIERS = ("ctrl", "shift", "alt", "win")

# Virtual-key codes (Windows). Letters/digits use their ASCII upper-case code.
VK = {
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08, "space": 0x20,
    "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "capslock": 0x14, "printscreen": 0x2C, "menu": 0x5D, "pause": 0x13,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}
# Keys that must be sent with KEYEVENTF_EXTENDEDKEY on Windows.
EXTENDED = {"delete", "insert", "home", "end", "pageup", "pagedown",
            "left", "up", "right", "down", "win", "menu", "printscreen"}

# Chords that are always treated as risky regardless of the model's opinion.
RISKY_CHORDS = {
    ("alt", "f4"), ("shift", "delete"), ("ctrl", "shift", "delete"),
    ("ctrl", "alt", "delete"), ("win", "l"), ("win", "r"),
}


def parse_chord(chord: str | list[str]) -> list[str]:
    """'Ctrl+Shift+S' -> ['ctrl', 'shift', 's'] with modifiers first, aliases resolved."""
    if isinstance(chord, str):
        raw = chord.replace(" ", "")
        # allow a literal '+' key: "ctrl++"
        if raw.endswith("++"):
            parts = raw[:-2].split("+") + ["+"]
        else:
            parts = raw.split("+")
    else:
        parts = list(chord)
    keys = [ALIASES.get(p.strip().lower(), p.strip().lower()) for p in parts if p.strip()]
    if not keys:
        raise ValueError(f"empty key chord: {chord!r}")
    mods = [k for k in MODIFIERS if k in keys]
    rest = [k for k in keys if k not in MODIFIERS]
    for k in rest:
        if k not in VK and len(k) != 1:
            raise ValueError(f"unknown key {k!r} in chord {chord!r}")
    return mods + rest


def is_risky_chord(keys: list[str]) -> bool:
    return tuple(keys) in RISKY_CHORDS
