"""Tk window for the Pac-Man demo: maze, HUD, and the policy's move probabilities as arrows.

Each tick: ask the policy (in a background asyncio loop), draw the probability arrows at
Pac-Man's position for `delay_ms`, then move. Keys: space = pause, r = restart, q/Esc = quit.
"""
from __future__ import annotations

import asyncio
import threading
import tkinter as tk
from concurrent.futures import Future
from typing import Any, Awaitable, Callable, Coroutine, Optional

from .pacman import DIRS, PacmanGame
from .pacman_agent import MoveDecision, Policy

CELL = 26
HUD_TOP, HUD_BOTTOM = 72, 64
WINDOW_POS = "+40+0"  # top of the screen, so the bottom panel clears the taskbar
POLL_MS = 15

BG = "#000000"
WALL = "#2121ff"
PELLET = "#ffb897"
PAC = "#ffff00"
ARROW_TOP, ARROW_OTHER = "#ffd23f", "#b8902a"
LABEL_RED, LABEL_BLUE, TEXT = "#ff3b3b", "#4f6bff", "#ffffff"
FRIGHT, FRIGHT_FLASH = "#2121ff", "#ffffff"
FONT = ("Courier New", 13, "bold")
FONT_SMALL = ("Courier New", 10, "bold")
MOUTH_ANGLE = {"right": 0, "up": 90, "left": 180, "down": 270}
ARROW_GLYPH = {"up": "↑", "down": "↓", "left": "←", "right": "→"}


class _AsyncWorker:
    """A private asyncio loop on a daemon thread, so HTTP calls never block Tk."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

    def submit(self, coro: Coroutine[Any, Any, Any]) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self, closer: Optional[Callable[[], Awaitable[None]]] = None) -> None:
        if closer is not None:
            try:
                self.submit(closer()).result(timeout=5)
            except Exception:
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)


class PacmanApp:
    def __init__(self, root: tk.Tk, policy: Policy, *, seed: Optional[int] = 0, delay_ms: int = 400,
                 max_steps: int = 600, closer: Optional[Callable[[], Awaitable[None]]] = None) -> None:
        self.root, self.policy, self.seed = root, policy, seed
        self.delay_ms, self.max_steps, self.closer = delay_ms, max_steps, closer
        self.worker = _AsyncWorker()
        self.high_score = 0
        self.paused = self.closed = False
        self._new_game()
        w, h = self.game.width * CELL, self.game.height * CELL
        self.canvas = tk.Canvas(root, width=w, height=h + HUD_TOP + HUD_BOTTOM, bg=BG, highlightthickness=0)
        self.canvas.pack()
        root.title(f"Pac-Man · {policy.name.title()}")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.close)
        for key, fn in (("<space>", self.toggle_pause), ("r", self.restart), ("q", self.close),
                        ("<Escape>", self.close)):
            root.bind(key, lambda _e, fn=fn: fn())
        self._draw_maze()
        self.redraw()
        root.after(300, self._tick)

    # ------------------------------------------------------------------ control
    def _new_game(self) -> None:
        self.game = PacmanGame(seed=self.seed)
        self.steps = self.calls = 0
        self.cost = self.latency_total = 0.0
        self.decision: Optional[MoveDecision] = None
        self.future: Optional[Future] = None
        self.status = ""

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.redraw()
        if not self.paused:
            self.root.after(POLL_MS, self._tick)

    def restart(self) -> None:
        if self.future is not None and not self.future.done():
            return  # let the pending decision finish first
        was_over = self.game.over or self.steps >= self.max_steps
        if self.seed is not None:
            self.seed += 1  # a different ghost pattern each round
        self._new_game()
        self.redraw()
        if was_over and not self.paused:
            self.root.after(300, self._tick)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.worker.stop(self.closer)
        self.root.destroy()

    def _tick(self) -> None:
        if self.closed or self.paused:
            return
        g = self.game
        if g.over or self.steps >= self.max_steps:
            self.redraw()
            return
        if self.future is None:
            self.future = self.worker.submit(self.policy.decide(g))
            self.root.after(POLL_MS, self._tick)
            return
        if not self.future.done():
            self.root.after(POLL_MS, self._tick)
            return
        try:
            d = self.future.result()
        except Exception as exc:  # show the error instead of dying silently
            self.future, self.status = None, f"policy error: {exc}"[:70]
            self.redraw()
            return
        self.future = None
        self.decision = d
        if d.source == "decision-1":
            self.calls += 1
            self.cost += d.cost or 0.0
            self.latency_total += d.latency_ms
        self.status = f"fallback: {d.error}"[:70] if d.source == "fallback" else ""
        self.redraw()
        hold = self.delay_ms if d.source != "forced" else self.delay_ms // 3
        self.root.after(max(1, hold), lambda: self._advance(d))

    def _advance(self, d: MoveDecision) -> None:
        if self.closed:
            return
        self.game.step(d.move)
        self.steps += 1
        self.high_score = max(self.high_score, self.game.score)
        self.decision = None
        self.redraw()
        self.root.after(POLL_MS, self._tick)

    # ------------------------------------------------------------------ drawing
    def _cell_box(self, p: Any, inset: float = 0.0) -> tuple:
        x0, y0 = p[0] * CELL + inset, HUD_TOP + p[1] * CELL + inset
        return x0, y0, x0 + CELL - 2 * inset, y0 + CELL - 2 * inset

    def _draw_maze(self) -> None:
        g = self.game
        for wx, wy in g.walls:
            x0, y0, x1, y1 = self._cell_box((wx, wy))
            for d, (dx, dy) in DIRS.items():
                nx, ny = wx + dx, wy + dy
                if (nx, ny) in g.walls or not (0 <= nx < g.width and 0 <= ny < g.height):
                    continue
                edge = {"up": (x0, y0, x1, y0), "down": (x0, y1, x1, y1),
                        "left": (x0, y0, x0, y1), "right": (x1, y0, x1, y1)}[d]
                self.canvas.create_line(*edge, fill=WALL, width=2)
        if g.door:
            x0, y0, x1, y1 = self._cell_box(g.door)
            self.canvas.create_line(x0, (y0 + y1) / 2, x1, (y0 + y1) / 2, fill="#ffb8de", width=3)

    def redraw(self) -> None:
        c, g = self.canvas, self.game
        c.delete("dyn")
        for p in g.pellets:
            x0, y0, x1, y1 = self._cell_box(p, CELL / 2 - 2.5)
            c.create_oval(x0, y0, x1, y1, fill=PELLET, outline="", tags="dyn")
        if g.tick % 6 < 4:
            for p in g.power:
                x0, y0, x1, y1 = self._cell_box(p, CELL / 2 - 7)
                c.create_oval(x0, y0, x1, y1, fill=PELLET, outline="", tags="dyn")
        for ghost in g.ghosts:
            self._draw_ghost(ghost)
        self._draw_pac()
        if self.decision is not None:
            self._draw_arrows(self.decision)
        self._draw_hud()

    def _draw_pac(self) -> None:
        g = self.game
        x0, y0, x1, y1 = self._cell_box(g.pac, 3)
        mouth = 35 if g.tick % 2 == 0 else 8
        start = MOUTH_ANGLE[g.pac_dir] + mouth
        self.canvas.create_arc(x0, y0, x1, y1, start=start, extent=360 - 2 * mouth,
                               fill=PAC, outline="", tags="dyn")

    def _draw_ghost(self, ghost: Any) -> None:
        c = self.canvas
        x0, y0, x1, y1 = self._cell_box(ghost.pos, 3)
        w = x1 - x0
        if ghost.edible:
            color = FRIGHT_FLASH if ghost.frightened < 6 and self.game.tick % 2 else FRIGHT
        else:
            color = ghost.color
        c.create_arc(x0, y0, x1, y0 + w, start=0, extent=180, fill=color, outline="", tags="dyn")
        mid = y0 + w / 2
        c.create_rectangle(x0, mid, x1, y1 - 4, fill=color, outline="", tags="dyn")
        skirt = [x0, y1 - 4, x0, y1, x0 + w / 4, y1 - 4, x0 + w / 2, y1, x0 + 3 * w / 4, y1 - 4, x1, y1, x1, y1 - 4]
        c.create_polygon(skirt, fill=color, outline="", tags="dyn")
        dx, dy = DIRS.get(ghost.direction, (0, 0))
        for ex in (x0 + w * 0.3, x0 + w * 0.7):
            ey = mid - 1
            if ghost.edible:
                c.create_oval(ex - 1.5, ey - 1.5, ex + 1.5, ey + 1.5, fill=PELLET, outline="", tags="dyn")
                continue
            c.create_oval(ex - 3.5, ey - 4, ex + 3.5, ey + 4, fill="white", outline="", tags="dyn")
            px, py = ex + dx * 1.8, ey + dy * 2
            c.create_oval(px - 1.8, py - 1.8, px + 1.8, py + 1.8, fill="#2121de", outline="", tags="dyn")

    def _draw_arrows(self, d: MoveDecision) -> None:
        c = self.canvas
        x0, y0, x1, y1 = self._cell_box(self.game.pac)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        for direction, p in sorted(d.probabilities.items(), key=lambda kv: kv[1]):
            dx, dy = DIRS[direction]
            top = direction == d.move
            length = CELL * (0.55 + 1.3 * p)
            width = 2 + 5 * p
            ex, ey = cx + dx * length, cy + dy * length
            c.create_line(cx, cy, ex, ey, fill=ARROW_TOP if top else ARROW_OTHER, width=width,
                          arrow=tk.LAST, arrowshape=(8 + 6 * p, 10 + 6 * p, 4 + 3 * p), tags="dyn")
            if top:
                lx, ly = ex + dx * 16 + (14 if dy else 0), ey + dy * 12 - (12 if dx else 0)
                c.create_text(lx, ly, text=f"{p:.0%}", fill=ARROW_TOP, font=FONT_SMALL, tags="dyn")

    def _draw_hud(self) -> None:
        c, g = self.canvas, self.game
        w = g.width * CELL
        c.create_text(10, 16, text=self.policy.name, anchor="w", fill=LABEL_RED, font=FONT_SMALL, tags="dyn")
        c.create_text(10, 42, text=f"{g.score:,}", anchor="w", fill=TEXT, font=FONT, tags="dyn")
        c.create_text(w / 2, 16, text="HIGH SCORE", fill=TEXT, font=FONT_SMALL, tags="dyn")
        c.create_text(w / 2, 42, text=f"{max(self.high_score, g.score):,}", fill=TEXT, font=FONT, tags="dyn")
        c.create_text(w - 10, 16, text="GHOSTS", anchor="e", fill=LABEL_BLUE, font=FONT_SMALL, tags="dyn")
        c.create_text(w - 10, 42, text="CLASSIC", anchor="e", fill=TEXT, font=FONT_SMALL, tags="dyn")
        base = HUD_TOP + g.height * CELL
        for i in range(max(0, g.lives - 1)):
            x = 14 + i * 26
            c.create_arc(x, base + 10, x + 20, base + 30, start=215, extent=290, fill=PAC, outline="", tags="dyn")
        avg = self.latency_total / self.calls if self.calls else 0.0
        stats = f"calls {self.calls}  avg {avg:.0f} ms  ${self.cost:.5f}  step {self.steps}"
        c.create_text(w - 10, base + 20, text=stats, anchor="e", fill=TEXT, font=FONT_SMALL, tags="dyn")
        if self.decision is not None:
            ranked = sorted(self.decision.probabilities.items(), key=lambda kv: kv[1], reverse=True)
            line = "  ".join(f"{ARROW_GLYPH[k]} {v:.0%}" for k, v in ranked)
            c.create_text(w / 2, base + 46, text=f"{line}   [{self.decision.source}]",
                          fill=ARROW_TOP, font=FONT_SMALL, tags="dyn")
        elif self.status:
            c.create_text(w / 2, base + 46, text=self.status, fill=LABEL_RED, font=FONT_SMALL, tags="dyn")
        banner = None
        if g.over:
            banner = "YOU WIN!" if g.won else "GAME OVER"
        elif self.steps >= self.max_steps:
            banner = "STEP LIMIT"
        elif self.paused:
            banner = "PAUSED"
        if banner:
            cy = HUD_TOP + g.height * CELL / 2
            c.create_rectangle(w / 2 - 120, cy - 34, w / 2 + 120, cy + 34, fill=BG, outline=WALL, width=2, tags="dyn")
            c.create_text(w / 2, cy - 8, text=banner, fill=PAC, font=FONT, tags="dyn")
            hint = "space: resume" if banner == "PAUSED" else "r: play again"
            c.create_text(w / 2, cy + 16, text=hint, fill=TEXT, font=FONT_SMALL, tags="dyn")


def run_app(policy: Policy, *, seed: Optional[int] = 0, delay_ms: int = 400, max_steps: int = 600,
            closer: Optional[Callable[[], Awaitable[None]]] = None) -> None:
    root = tk.Tk()
    root.geometry(WINDOW_POS)
    PacmanApp(root, policy, seed=seed, delay_ms=delay_ms, max_steps=max_steps, closer=closer)
    # Started from a terminal, the window can open behind other apps: raise it once.
    root.lift()
    root.attributes("-topmost", True)
    root.after(500, lambda: root.attributes("-topmost", False))
    root.mainloop()
