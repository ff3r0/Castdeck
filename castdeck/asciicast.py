"""Lecture streaming des fichiers asciicast v2 et v3.

Les fichiers ne sont jamais modifiés : tout est lu ligne par ligne, et la
conversion v3 -> v2 (ou la compression des temps morts) se fait à la volée.

v2 : events ``[t_absolu, code, data]`` (secondes depuis le début).
v3 : events ``[intervalle, code, data]`` (secondes depuis l'event précédent),
     lignes ``#`` = commentaires, header ``term: {cols, rows, ...}``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator

SNIFF_BYTES = 64 * 1024
# Préfixe d'un event : permet de lire le temps et le code sans json.loads.
_EVENT_PREFIX = re.compile(r'^\[\s*([-+0-9.eE]+)\s*,\s*"([a-z])"')

Event = tuple[float, str, str]


class CastError(ValueError):
    pass


@dataclass
class Header:
    version: int
    cols: int
    rows: int
    timestamp: float | None
    idle_time_limit: float | None
    raw: dict

    @classmethod
    def from_dict(cls, d: dict) -> "Header":
        version = d.get("version")
        if version == 2:
            cols, rows = d.get("width"), d.get("height")
        elif version == 3:
            term = d.get("term") or {}
            cols, rows = term.get("cols"), term.get("rows")
        else:
            raise CastError(f"version asciicast non supportée : {version!r}")
        if not isinstance(cols, int) or not isinstance(rows, int):
            raise CastError("dimensions absentes du header")
        ts = d.get("timestamp")
        itl = d.get("idle_time_limit")
        return cls(
            version=version,
            cols=cols,
            rows=rows,
            timestamp=float(ts) if isinstance(ts, (int, float)) else None,
            idle_time_limit=float(itl) if isinstance(itl, (int, float)) else None,
            raw=d,
        )

    def to_v2(self) -> dict:
        """Header v2 servi au player. ``idle_time_limit`` est volontairement
        retiré : sinon asciinema-player compresse le temps de lui-même et
        seek()/getCurrentTime() ne sont plus en temps d'origine."""
        out = {"version": 2, "width": self.cols, "height": self.rows}
        if self.timestamp is not None:
            out["timestamp"] = int(self.timestamp)
        if self.version == 2:
            for k in ("env", "theme", "title", "command"):
                if k in self.raw:
                    out[k] = self.raw[k]
        else:
            term = self.raw.get("term") or {}
            if term.get("type"):
                out["env"] = {"TERM": term["type"]}
            if term.get("theme"):
                out["theme"] = term["theme"]
            for k in ("title", "command"):
                if k in self.raw:
                    out[k] = self.raw[k]
        return out


def _first_line(path: Path) -> str | None:
    try:
        with open(path, "rb") as f:
            chunk = f.read(SNIFF_BYTES)
    except OSError:
        return None
    line = chunk.split(b"\n", 1)[0].strip()
    if not line.startswith(b"{"):
        return None
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        return None


def sniff(path: str | Path) -> Header | None:
    """Header si le fichier est un asciicast v2/v3 (détection par contenu,
    pas par extension), sinon None."""
    line = _first_line(Path(path))
    if line is None:
        return None
    try:
        d = json.loads(line)
        if not isinstance(d, dict):
            return None
        return Header.from_dict(d)
    except (ValueError, CastError):
        return None


def read_header(path: str | Path) -> Header:
    h = sniff(path)
    if h is None:
        raise CastError(f"{path} : pas un fichier asciicast v2/v3")
    return h


def _iter_raw_lines(path: Path) -> Iterator[str]:
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        f.readline()  # header
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                yield line


def iter_events(path: str | Path, header: Header | None = None) -> Iterator[Event]:
    """Events en temps ABSOLU (secondes depuis le début), quelle que soit la
    version. Les lignes corrompues (ex. dernière ligne tronquée quand
    l'enregistrement a été tué) sont ignorées."""
    path = Path(path)
    header = header or read_header(path)
    relative = header.version == 3
    t = 0.0
    for line in _iter_raw_lines(path):
        try:
            ev = json.loads(line)
            ts, code = float(ev[0]), ev[1]
            data = ev[2] if len(ev) > 2 else ""
        except (ValueError, IndexError, TypeError):
            continue
        if not isinstance(data, str):
            data = json.dumps(data)
        t = t + ts if relative else ts
        yield (t, code, data)


@dataclass
class CastInfo:
    header: Header
    duration: float
    n_events: int
    markers: list[tuple[float, str]] = field(default_factory=list)


def scan(path: str | Path) -> CastInfo:
    """Parcours rapide : durée, nombre d'events, markers. N'appelle json.loads
    que sur les markers (le reste se lit par regex sur le préfixe)."""
    path = Path(path)
    header = read_header(path)
    relative = header.version == 3
    t = 0.0
    n = 0
    markers: list[tuple[float, str]] = []
    for line in _iter_raw_lines(path):
        m = _EVENT_PREFIX.match(line)
        if not m:
            continue
        try:
            ts = float(m.group(1))
        except ValueError:
            continue
        t = t + ts if relative else ts
        n += 1
        if m.group(2) == "m":
            label = ""
            try:
                ev = json.loads(line)
                label = ev[2] if len(ev) > 2 and isinstance(ev[2], str) else ""
            except ValueError:
                pass
            markers.append((t, label))
    return CastInfo(header=header, duration=t, n_events=n, markers=markers)


def parse_resize(data: str) -> tuple[int, int] | None:
    """``"COLSxROWS"`` -> (cols, rows)."""
    try:
        c, r = data.lower().split("x", 1)
        return int(c), int(r)
    except ValueError:
        return None


# --- compression des temps morts ------------------------------------------
#
# Même algorithme que le timeLimiter d'asciinema-player : tout délai entre deux
# events supérieur à `limit` est ramené à `limit`. On le fait côté serveur
# pour maîtriser exactement la correspondance temps d'origine <-> temps affiché.


def compress_times(times: Iterable[float], limit: float) -> Iterator[float]:
    prev = 0.0
    shift = 0.0
    for t in times:
        delta = (t - prev) - limit
        prev = t
        if delta > 0:
            shift += delta
        yield t - shift


def time_knots(times: Iterable[float], limit: float) -> list[tuple[float, float]]:
    """Points (t_origine, t_affiché) d'une fonction affine par morceaux,
    croissante, de pente 1 ou 0. Au-delà du dernier point : pente 1.

    Un trou [prev, t] plus long que `limit` donne deux points :
    (prev+limit, d+limit) puis (t, d+limit) — palier où le temps affiché
    n'avance plus."""
    knots: list[tuple[float, float]] = [(0.0, 0.0)]
    prev = 0.0
    shift = 0.0
    for t in times:
        gap = t - prev
        if gap > limit:
            plateau = prev - shift + limit
            knots.append((prev + limit, plateau))
            shift += gap - limit
            # même valeur exacte pour les deux bornes du palier (pas de t-shift)
            knots.append((t, plateau))
        prev = t
    return knots


def orig_to_display(knots: list[tuple[float, float]], x: float) -> float:
    lo, hi = 0, len(knots) - 1
    if x <= knots[0][0]:
        return x
    # dernier point dont l'abscisse <= x
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if knots[mid][0] <= x:
            lo = mid
        else:
            hi = mid - 1
    o, d = knots[lo]
    if lo + 1 < len(knots):
        o2, d2 = knots[lo + 1]
        if d2 == d:  # palier
            return d
    return d + (x - o)


def display_to_orig(knots: list[tuple[float, float]], y: float) -> float:
    """Inverse. Sur un palier on renvoie la FIN du palier : c'est l'event que
    le player joue à cet instant affiché."""
    if y <= 0:
        return y
    best = None
    for i, (o, d) in enumerate(knots):
        if d <= y:
            best = i
        else:
            break
    o, d = knots[best]
    # si ce point ouvre un palier, il faut sauter à sa fin
    while best + 1 < len(knots) and knots[best + 1][1] == d:
        best += 1
        o, d = knots[best]
    return o + (y - d)


# --- conversion vers v2 pour le player --------------------------------------


def to_v2_lines(
    path: str | Path,
    idle_limit: float | None = None,
    transform: Callable[[str], str] | None = None,
) -> Iterator[str]:
    """Lignes (avec \\n) d'un asciicast v2 équivalent, prêtes à être servies
    au player. Optionnellement compressé et/ou passé dans `transform`
    (redaction) pour les events "o"/"i"."""
    path = Path(path)
    header = read_header(path)
    yield json.dumps(header.to_v2(), ensure_ascii=False) + "\n"
    events = iter_events(path, header)
    if idle_limit is not None and idle_limit > 0:
        events = _compress_events(events, idle_limit)
    for t, code, data in events:
        if code == "x":  # event v3 "exit", inconnu en v2
            continue
        if transform is not None and code in ("o", "i"):
            data = transform(data)
        yield json.dumps([round(t, 6), code, data], ensure_ascii=False) + "\n"


def _compress_events(events: Iterable[Event], limit: float) -> Iterator[Event]:
    prev = 0.0
    shift = 0.0
    for t, code, data in events:
        delta = (t - prev) - limit
        prev = t
        if delta > 0:
            shift += delta
        yield (t - shift, code, data)
