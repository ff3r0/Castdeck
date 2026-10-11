"""Commandes tapées : lecture de commands.log et rattachement aux sessions.

Format : ``YYYY-MM-DD HH:MM:SS [ctx] commande`` en heure LOCALE, ctx = ``cli``
ou id de pane tmux (``%3``). Les lignes qui ne matchent pas le format sont
considérées comme la suite (multi-ligne) de la commande précédente.
"""
from __future__ import annotations

import bisect
import re
import sys
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Iterable

_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) \[([^\]]*)\] ?(.*)$")

# Tolérance (s) pour rattacher une commande tapée juste avant/après les bornes.
ATTACH_TOLERANCE = 1.0


@dataclass
class Command:
    abs_time: float          # epoch UTC
    ctx: str
    cmd: str
    source: str = "commands.log"
    session_id: str | None = None
    offset: float | None = None  # secondes depuis le début de la session (temps d'origine)


def resolve_tz(name: str | None) -> tzinfo | None:
    """None = fuseau du système (datetime.astimezone)."""
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # tzdata absent dans certains conteneurs
        print(f"castdeck: fuseau {name!r} introuvable, fuseau système utilisé", file=sys.stderr)
        return None


def local_to_epoch(naive: datetime, tz: tzinfo | None) -> float:
    if tz is None:
        return naive.astimezone().timestamp()
    return naive.replace(tzinfo=tz).timestamp()


def parse_commands_log(path: str | Path, tz: tzinfo | None = None) -> list[Command]:
    out: list[Command] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\r\n")
            m = _LINE.match(line)
            if m:
                try:
                    naive = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                cmd = m.group(4).strip()
                if cmd:
                    out.append(Command(local_to_epoch(naive, tz), m.group(3).strip() or "cli", cmd))
            elif out and line.strip():
                out[-1].cmd += "\n" + line
    out.sort(key=lambda c: c.abs_time)
    return out


@dataclass
class Span:
    session_id: str
    start: float
    end: float


def attach(commands: Iterable[Command], spans: list[Span]) -> list[Command]:
    """Rattache chaque commande à la session dont [début, fin] contient
    l'instant (± tolérance). En cas de chevauchement, la session la plus
    récemment démarrée gagne. Les commandes hors session gardent
    session_id=None."""
    spans = sorted(spans, key=lambda s: s.start)
    starts = [s.start for s in spans]
    result = []
    for c in commands:
        c.session_id = None
        c.offset = None
        i = bisect.bisect_right(starts, c.abs_time + ATTACH_TOLERANCE) - 1
        while i >= 0:
            s = spans[i]
            if s.start - ATTACH_TOLERANCE <= c.abs_time <= s.end + ATTACH_TOLERANCE:
                c.session_id = s.session_id
                c.offset = min(max(c.abs_time - s.start, 0.0), s.end - s.start)
                break
            i -= 1
            # une session antérieure ne peut contenir t que si elle dure assez :
            # on continue simplement, le nombre de sessions est faible.
        result.append(c)
    return result
