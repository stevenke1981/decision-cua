"""Pac-Man game logic for the Decision-1 demo (no UI, no I/O).

Coordinates are (x, y) with y growing downwards. One `step()` moves Pac-Man one cell and
every active ghost at most one cell, so a turn-based policy can pick a direction each tick.
"""
from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

Pos = Tuple[int, int]

# '#' wall, '.' pellet, 'o' power pellet, 'P' Pac-Man start, 'G' ghost house, '-' house door.
# Row 9 is open at both edges: a tunnel that wraps around.
MAZE = (
    "###################",
    "#........#........#",
    "#o##.###.#.###.##o#",
    "#.................#",
    "#.##.#.#####.#.##.#",
    "#....#...#...#....#",
    "####.### # ###.####",
    "   #.#       #.#   ",
    "####.# ##-## #.####",
    "    .  #GGG#  .    ",
    "####.# ##### #.####",
    "   #.#       #.#   ",
    "####.# ##### #.####",
    "#........#........#",
    "#.##.###.#.###.##.#",
    "#o.#.....P.....#.o#",
    "##.#.#.#####.#.#.##",
    "#....#...#...#....#",
    "#.######.#.######.#",
    "#.................#",
    "###################",
)

DIRS: Dict[str, Pos] = {"up": (0, -1), "left": (-1, 0), "down": (0, 1), "right": (1, 0)}
OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}

PELLET_POINTS, POWER_POINTS, GHOST_POINTS = 10, 50, 200
FRIGHTENED_TICKS = 24
START_LIVES = 3
RANDOM_GHOST_MOVE = 0.2      # chance a chasing ghost picks a random turn (keeps games varied)
EATEN_GHOST_DELAY = 10       # ticks an eaten ghost waits in the house
INF = 10**6

# name, colour, start cell, release delay (ticks)
GHOST_SPECS = (
    ("blinky", "#ff0000", (9, 7), 0),
    ("pinky", "#ffb8ff", (9, 9), 8),
    ("inky", "#00ffff", (8, 9), 20),
    ("clyde", "#ffb852", (10, 9), 32),
)
GHOST_LETTERS = {"blinky": "B", "pinky": "K", "inky": "I", "clyde": "C"}


@dataclass
class Ghost:
    name: str
    color: str
    home: Pos
    pos: Pos
    release_tick: int
    in_house: bool
    direction: str = "left"
    frightened: int = 0  # ticks left

    @property
    def dangerous(self) -> bool:
        return not self.in_house and self.frightened == 0

    @property
    def edible(self) -> bool:
        return not self.in_house and self.frightened > 0


@dataclass(frozen=True)
class MoveInfo:
    """What a move leads to, measured from the cell Pac-Man would step onto."""

    direction: str
    target: Pos
    pellet: bool
    power: bool
    pellet_dist: int   # steps to the nearest pellet (0 = the target cell has one)
    ghost_dist: int    # steps to the nearest dangerous ghost (INF if none can reach)
    edible_dist: int   # steps to the nearest frightened ghost (INF if none)


class PacmanGame:
    def __init__(self, seed: Optional[int] = 0, maze: Tuple[str, ...] = MAZE) -> None:
        self.rng = random.Random(seed)
        self.width, self.height = len(maze[0]), len(maze)
        self.walls: set = set()
        self.pellets: set = set()
        self.power: set = set()
        self.house: set = set()
        self.door: Optional[Pos] = None
        self.pac_start: Pos = (0, 0)
        for y, row in enumerate(maze):
            if len(row) != self.width:
                raise ValueError(f"maze row {y} has width {len(row)}, expected {self.width}")
            for x, ch in enumerate(row):
                p = (x, y)
                if ch == "#":
                    self.walls.add(p)
                elif ch == ".":
                    self.pellets.add(p)
                elif ch == "o":
                    self.power.add(p)
                elif ch == "P":
                    self.pac_start = p
                elif ch == "G":
                    self.house.add(p)
                elif ch == "-":
                    self.door = p
        self.house_center: Pos = sorted(self.house)[len(self.house) // 2] if self.house else self.pac_start
        self.exit: Pos = (self.door[0], self.door[1] - 1) if self.door else self.pac_start
        self.score = 0
        self.lives = START_LIVES
        self.tick = 0
        self.over = False
        self.won = False
        self.events: List[str] = []
        self._combo = 0
        self.pac: Pos = self.pac_start
        self.pac_dir = "left"
        self.ghosts: List[Ghost] = []
        self._reset_positions()

    # ------------------------------------------------------------------ geometry
    def neighbor(self, p: Pos, direction: str) -> Pos:
        dx, dy = DIRS[direction]
        return ((p[0] + dx) % self.width, p[1] + dy)

    def is_open(self, p: Pos) -> bool:
        """Walkable for Pac-Man and for ghosts outside the house."""
        return (0 <= p[1] < self.height and p not in self.walls
                and p not in self.house and p != self.door)

    def legal_moves(self, p: Optional[Pos] = None) -> List[str]:
        p = self.pac if p is None else p
        return [d for d in DIRS if self.is_open(self.neighbor(p, d))]

    def distances(self, start: Pos) -> Dict[Pos, int]:
        """Breadth-first step counts from `start` over walkable cells (tunnel included)."""
        dist = {start: 0}
        queue = deque([start])
        while queue:
            p = queue.popleft()
            for d in DIRS:
                n = self.neighbor(p, d)
                if n not in dist and self.is_open(n):
                    dist[n] = dist[p] + 1
                    queue.append(n)
        return dist

    def move_info(self, direction: str) -> MoveInfo:
        target = self.neighbor(self.pac, direction)
        dist = self.distances(target)
        food = self.pellets | self.power
        return MoveInfo(
            direction=direction,
            target=target,
            pellet=target in self.pellets,
            power=target in self.power,
            pellet_dist=min((dist[f] for f in food if f in dist), default=INF),
            ghost_dist=min((dist.get(g.pos, INF) for g in self.ghosts if g.dangerous), default=INF),
            edible_dist=min((dist.get(g.pos, INF) for g in self.ghosts if g.edible), default=INF),
        )

    # ------------------------------------------------------------------ simulation
    def step(self, direction: Optional[str] = None) -> List[str]:
        """Advance one tick. An illegal or missing direction keeps the current heading."""
        if direction is not None and direction not in DIRS:
            raise ValueError(f"unknown direction {direction!r}")
        self.events = []
        if self.over:
            return self.events
        legal = self.legal_moves()
        if direction in legal:
            self.pac_dir = direction
        pac_prev = self.pac
        if self.pac_dir in legal:
            self.pac = self.neighbor(self.pac, self.pac_dir)
        self._eat()
        if not self._collide():
            ghost_prev = {g.name: g.pos for g in self.ghosts}
            self._move_ghosts()
            self._collide(pac_prev, ghost_prev)
        self.tick += 1
        for g in self.ghosts:
            if g.frightened:
                g.frightened -= 1
        if not self.over and not self.pellets and not self.power:
            self.won = self.over = True
            self.events.append("win")
        return self.events

    def _reset_positions(self) -> None:
        self.pac, self.pac_dir = self.pac_start, "left"
        self.ghosts = [
            Ghost(name, color, home, home, self.tick + delay, in_house=home in self.house)
            for name, color, home, delay in GHOST_SPECS
        ]

    def _eat(self) -> None:
        if self.pac in self.pellets:
            self.pellets.discard(self.pac)
            self.score += PELLET_POINTS
            self.events.append("pellet")
        elif self.pac in self.power:
            self.power.discard(self.pac)
            self.score += POWER_POINTS
            self._combo = 0
            for g in self.ghosts:
                if not g.in_house:
                    g.frightened = FRIGHTENED_TICKS
                    g.direction = OPPOSITE[g.direction]
            self.events.append("power")

    def _collide(self, pac_prev: Optional[Pos] = None,
                 ghost_prev: Optional[Dict[str, Pos]] = None) -> bool:
        """Resolve Pac-Man/ghost contact. Returns True when Pac-Man died."""
        for g in self.ghosts:
            if g.in_house:
                continue
            swapped = (pac_prev is not None and ghost_prev is not None
                       and ghost_prev.get(g.name) == self.pac and g.pos == pac_prev)
            if g.pos != self.pac and not swapped:
                continue
            if g.frightened:
                self.score += GHOST_POINTS * 2 ** self._combo
                self._combo += 1
                g.pos, g.in_house, g.frightened = self.house_center, True, 0
                g.release_tick = self.tick + EATEN_GHOST_DELAY
                self.events.append(f"eat-ghost:{g.name}")
                continue
            self.lives -= 1
            self.events.append("death")
            if self.lives <= 0:
                self.over = True
                self.events.append("game-over")
            else:
                self._reset_positions()
            return True
        return False

    def _move_ghosts(self) -> None:
        for g in self.ghosts:
            if g.in_house:
                if self.tick >= g.release_tick:
                    g.pos, g.in_house, g.direction = self.exit, False, "left"
                continue
            if g.frightened and self.tick % 2:
                continue  # frightened ghosts move at half speed
            if not g.frightened and self.tick % 5 == 4:
                continue  # ghosts are slightly slower than Pac-Man
            options = self.legal_moves(g.pos)
            forward = [d for d in options if d != OPPOSITE[g.direction]] or options
            if not forward:
                continue
            if g.frightened or self.rng.random() < RANDOM_GHOST_MOVE:
                choice = self.rng.choice(forward)
            else:
                tx, ty = self._ghost_target(g)

                def gap(d: str) -> int:
                    nx, ny = self.neighbor(g.pos, d)
                    return (nx - tx) ** 2 + (ny - ty) ** 2

                choice = min(forward, key=gap)
            g.direction = choice
            g.pos = self.neighbor(g.pos, choice)

    def _ghost_target(self, g: Ghost) -> Pos:
        px, py = self.pac
        dx, dy = DIRS[self.pac_dir]
        if g.name == "pinky":
            return (px + 4 * dx, py + 4 * dy)
        if g.name == "inky":
            bx, by = next((o.pos for o in self.ghosts if o.name == "blinky"), self.pac)
            ax, ay = px + 2 * dx, py + 2 * dy
            return (2 * ax - bx, 2 * ay - by)
        if g.name == "clyde" and (g.pos[0] - px) ** 2 + (g.pos[1] - py) ** 2 <= 64:
            return (0, self.height - 1)  # clyde retreats to his corner when close
        return self.pac

    # ------------------------------------------------------------------ text state
    def grid(self) -> List[str]:
        rows = []
        for y in range(self.height):
            row = []
            for x in range(self.width):
                p = (x, y)
                if p in self.walls:
                    row.append("#")
                elif p == self.door:
                    row.append("-")
                elif p in self.pellets:
                    row.append(".")
                elif p in self.power:
                    row.append("o")
                else:
                    row.append(" ")
            rows.append(row)
        for g in self.ghosts:
            x, y = g.pos
            rows[y][x] = "F" if g.edible else GHOST_LETTERS.get(g.name, "G")
        rows[self.pac[1]][self.pac[0]] = "P"
        return ["".join(r) for r in rows]

    def to_text(self) -> str:
        """The game state as plain text for Decision-1."""
        dist = self.distances(self.pac)
        lines = [
            "Pac-Man maze (x grows right, y grows down; row 9 wraps left/right).",
            "Legend: # wall, . pellet (10 pts), o power pellet (50 pts, makes ghosts edible), "
            "P Pac-Man, B/K/I/C dangerous ghosts, F frightened ghost (edible, 200+ pts).",
            *self.grid(),
            f"Pac-Man at x={self.pac[0]}, y={self.pac[1]}, heading {self.pac_dir}. "
            f"Score {self.score}, lives {self.lives}, pellets left {len(self.pellets) + len(self.power)}.",
        ]
        for g in self.ghosts:
            if g.in_house:
                lines.append(f"{g.name}: waiting in the ghost house.")
                continue
            state = f"frightened for {g.frightened} more ticks" if g.edible else "dangerous"
            steps = dist.get(g.pos, INF)
            reach = f"{steps} steps away" if steps < INF else "unreachable"
            lines.append(f"{g.name} at x={g.pos[0]}, y={g.pos[1]}: {state}, {reach}.")
        lines.append(f"Legal moves: {', '.join(self.legal_moves())}.")
        return "\n".join(lines)
