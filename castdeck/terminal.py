"""Rejeu des sessions dans pyte : snapshots texte de l'écran et détection
heuristique des commandes (fallback quand commands.log est absent).

Pour ne rien perdre de ce qui défile vite (sortie de nmap...), l'écran capture
les lignes qui sortent par le haut. Chaque snapshot ne contient que les
lignes NOUVELLES par rapport au snapshot précédent : une ligne restée affichée
n'est indexée qu'une fois.

Limite connue : sous tmux l'écran est composite, les snapshots mélangent les
panes.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import asciicast as ac

try:
    import pyte
except ImportError:  # pragma: no cover - dégradé sans pyte
    pyte = None

DEFAULT_PROMPT_REGEX = r"^\[.*?\] \S+ \S+ # (.+)$"
MAX_SCROLL_LINES = 3000  # flush anticipé d'un snapshot si ça défile énormément
# Anti-flood (cat d'un gros fichier...) : au plus N caractères indexés par
# intervalle de snapshot, le reste est ignoré pour garder un index raisonnable.
MAX_CHARS_PER_TICK = 256_000
TRUNCATED_NOTE = "[castdeck] … sortie volumineuse tronquée dans l'index"


def available() -> bool:
    return pyte is not None


@dataclass
class Snapshot:
    offset: float
    text: str


@dataclass
class DetectedCommand:
    offset: float
    cmd: str


if pyte is not None:

    class _Screen(pyte.Screen):
        """Screen qui garde les lignes sorties par le haut."""

        def __init__(self, cols: int, rows: int):
            super().__init__(cols, rows)
            self.scrolled: list[str] = []

        def index(self):
            top, bottom = self.margins or (0, self.lines - 1)
            if self.cursor.y == bottom and top == 0:
                self.scrolled.append(render_row(self, 0))
            super().index()

        # pyte n'implémente pas l'écran alternatif ; on ignore proprement
        def set_title(self, param):
            pass

        def set_icon_name(self, param):
            pass


def render_row(screen, y: int) -> str:
    row = screen.buffer[y]
    return "".join(row[x].data for x in range(screen.columns)).rstrip()


def screen_lines(screen) -> list[str]:
    return [render_row(screen, y) for y in range(screen.lines)]


class Replayer:
    def __init__(self, cols: int, rows: int, prompt_regex: str | None = DEFAULT_PROMPT_REGEX):
        if pyte is None:
            raise RuntimeError("pyte n'est pas installé")
        self.screen = _Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)
        self.prompt = re.compile(prompt_regex) if prompt_regex else None
        self.commands: list[DetectedCommand] = []

    def feed(self, t: float, code: str, data: str) -> None:
        if code == "r":
            size = ac.parse_resize(data)
            if size:
                self.screen.resize(lines=size[1], columns=size[0])
            return
        if code != "o":
            return
        if self.prompt is not None:
            i = data.find("\n")
            if i >= 0:
                # l'Enter est en général le 1er \n de son chunk : on lit la
                # ligne du curseur juste avant qu'il soit appliqué
                self.stream.feed(data[:i])
                self._detect(t)
                data = data[i:]
        self.stream.feed(data)

    def _detect(self, t: float) -> None:
        s = self.screen
        y = s.cursor.y
        line = render_row(s, y)
        m = self.prompt.match(line)
        if not m:
            # commande longue repliée sur plusieurs lignes : on remonte tant
            # que la ligne du dessus est pleine
            joined = line
            k = y - 1
            while k >= 0 and y - k <= 6:
                above = "".join(s.buffer[k][x].data for x in range(s.columns))
                if above.rstrip() == "" or len(above.rstrip()) < s.columns:
                    break
                joined = above + joined
                m = self.prompt.match(joined)
                if m:
                    break
                k -= 1
        if m:
            cmd = m.group(1).strip()
            last = self.commands[-1] if self.commands else None
            # sous tmux le même prompt peut être relu deux fois (écran composite)
            if cmd and not (last and last.cmd == cmd and t - last.offset < 2):
                self.commands.append(DetectedCommand(t, cmd))

    def text(self) -> str:
        return "\n".join(screen_lines(self.screen)).rstrip()


def replay(
    path: str | Path,
    interval: float = 5.0,
    extra_times: Iterable[float] = (),
    prompt_regex: str | None = DEFAULT_PROMPT_REGEX,
    on_snapshot: Callable[[Snapshot], None] | None = None,
) -> tuple[list[Snapshot], list[DetectedCommand]]:
    """Rejoue la session : snapshots toutes les `interval` secondes et à
    chaque instant de `extra_times` (commandes), + commandes détectées.

    Avec `on_snapshot`, les snapshots sont passés au fil de l'eau (mémoire
    bornée sur les longues sessions) et la liste renvoyée est vide."""
    header = ac.read_header(path)
    r = Replayer(header.cols, header.rows, prompt_regex)
    extra = sorted(set(t for t in extra_times if t >= 0))
    snaps: list[Snapshot] = []
    prev_screen: Counter = Counter()
    window_start: float | None = None  # 1er event "o" depuis le dernier snapshot
    next_tick = interval
    ei = 0
    tick_chars = 0  # caractères indexés depuis le dernier tick régulier
    emit = on_snapshot or snaps.append

    def take(at: float) -> None:
        nonlocal prev_screen, window_start, tick_chars
        if window_start is None:
            return
        current = screen_lines(r.screen)
        cur_count = Counter(l for l in current if l.strip())
        budget = prev_screen.copy()
        new_lines = []
        for l in r.screen.scrolled + current:
            if not l.strip():
                continue
            if budget[l] > 0:
                budget[l] -= 1  # déjà indexée au snapshot précédent
            else:
                new_lines.append(l)
        r.screen.scrolled.clear()
        prev_screen = cur_count
        if new_lines and tick_chars < MAX_CHARS_PER_TICK:
            text = "\n".join(new_lines)
            if tick_chars + len(text) > MAX_CHARS_PER_TICK:
                cut = text.rfind("\n", 0, MAX_CHARS_PER_TICK - tick_chars)
                text = text[: max(cut, 0)] + "\n" + TRUNCATED_NOTE
            tick_chars += len(text)
            emit(Snapshot(window_start, text))
        window_start = None

    for t, code, data in ac.iter_events(path, header):
        while True:
            due = min(next_tick, extra[ei] if ei < len(extra) else float("inf"))
            if t <= due:
                break
            take(due)
            if due == next_tick:
                tick_chars = 0
                next_tick += interval
                while next_tick < t - interval:  # saute les longues pauses
                    next_tick += interval
            else:
                ei += 1
        if code == "o" and window_start is None:
            window_start = t
        r.feed(t, code, data)
        if len(r.screen.scrolled) > MAX_SCROLL_LINES:
            take(t)
    take(float("inf"))
    return snaps, r.commands


def screen_at(path: str | Path, offset: float) -> str:
    """Texte de l'écran à l'instant `offset` (temps d'origine)."""
    header = ac.read_header(path)
    r = Replayer(header.cols, header.rows, prompt_regex=None)
    for t, code, data in ac.iter_events(path, header):
        if t > offset:
            break
        r.feed(t, code, data)
    return r.text()
