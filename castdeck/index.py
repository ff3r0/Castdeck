"""Index SQLite + FTS5 des sessions, commandes et sorties.

Deux phases :
  * synchrone et rapide (au lancement) : sessions, commandes de commands.log,
    panes tmux -> l'UI est utilisable tout de suite ;
  * en tâche de fond : rejeu pyte (snapshots des sorties + commandes
    heuristiques). La progression est exposée par /api/status.

Cache : <dossier>/.castdeck/index.db, invalidé par mtime+taille de chaque
fichier (et par les options qui changent le contenu de l'index).
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import asciicast as ac
from . import commands as cm
from . import terminal
from .redact import redact as _redact

SCHEMA_VERSION = 4
PANE_CHUNK_LINES = 40


@dataclass
class Config:
    root: Path
    data_dir: Path | None = None
    tz: str | None = None
    redact: bool = False
    prompt_regex: str | None = terminal.DEFAULT_PROMPT_REGEX
    snapshot_interval: float = 5.0
    idle_limit: float = 2.0

    def fingerprint(self) -> str:
        return json.dumps([SCHEMA_VERSION, self.tz, self.redact, self.prompt_regex,
                           self.snapshot_interval])


def default_data_dir(root: Path) -> Path:
    """<dossier>/.castdeck, ou ~/.cache/castdeck/<hash> si le dossier de logs
    n'est pas inscriptible (montage en lecture seule)."""
    d = root / ".castdeck"
    try:
        d.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=d):
            pass
        return d
    except OSError:
        h = hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:12]
        alt = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "castdeck" / h
        alt.mkdir(parents=True, exist_ok=True)
        print(f"castdeck: {d} non inscriptible, index dans {alt}", file=sys.stderr)
        return alt


def session_id_for(rel: str) -> str:
    return hashlib.sha1(rel.encode()).hexdigest()[:12]


def _fts_tokenizer(conn: sqlite3.Connection) -> str:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._probe USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE temp._probe")
        return "trigram"
    except sqlite3.OperationalError:
        return "unicode61"


class Index:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = cfg.root.resolve()
        self.data_dir = cfg.data_dir or default_data_dir(self.root)
        self.db_path = self.data_dir / "index.db"
        self.tz = cm.resolve_tz(cfg.tz)
        self._status_lock = threading.Lock()
        self.status = {"phase": "init", "total": 0, "done": 0, "current": None,
                       "outputs_available": terminal.available(), "error": None}
        self._thread: threading.Thread | None = None
        self.tokenizer = "trigram"

    # -- connexion ---------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        self.tokenizer = _fts_tokenizer(conn)
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS sessions(
                id TEXT PRIMARY KEY, file TEXT UNIQUE, version INT, start REAL,
                duration REAL, cols INT, rows INT, n_events INT, idle_time_limit REAL,
                markers TEXT, mtime REAL, size INT, outputs_done INT DEFAULT 0);
            CREATE TABLE IF NOT EXISTS commands(
                session_id TEXT, offset REAL, abs_time REAL, ctx TEXT, cmd TEXT, source TEXT);
            CREATE INDEX IF NOT EXISTS commands_session ON commands(session_id, offset);
            CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, kind TEXT, mtime REAL, size INT);
            CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
                text, kind UNINDEXED, session_id UNINDEXED, offset UNINDEXED,
                abs_time UNINDEXED, ref UNINDEXED, tokenize='{self.tokenizer}');
        """)
        conn.execute("INSERT OR REPLACE INTO meta VALUES('tokenizer', ?)", (self.tokenizer,))

    def _meta(self, conn, key):
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, conn, key, value):
        conn.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (key, value))

    def _set_status(self, **kw):
        with self._status_lock:
            self.status.update(kw)

    def get_status(self) -> dict:
        with self._status_lock:
            return dict(self.status)

    def _r(self, text: str) -> str:
        return _redact(text) if self.cfg.redact else text

    # -- phase synchrone ---------------------------------------------------

    def _scan_files(self):
        casts, panes, cmdlog = [], [], None
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                p = Path(dirpath) / name
                if name == "commands.log":
                    if cmdlog is None or p.parent == self.root:
                        cmdlog = p
                elif name.startswith("panes_") and name.endswith(".log"):
                    panes.append(p)
                elif not name.startswith("."):
                    header = ac.sniff(p)
                    if header is not None:
                        casts.append((p, header))
        return casts, panes, cmdlog

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def build(self, force: bool = False) -> None:
        t0 = time.time()
        if force and self.db_path.exists():
            for suffix in ("", "-wal", "-shm"):
                Path(str(self.db_path) + suffix).unlink(missing_ok=True)
        conn = self.connect()
        try:
            self._create_schema(conn)
            if self._meta(conn, "config") != self.cfg.fingerprint():
                conn.executescript("""
                    DELETE FROM sessions; DELETE FROM commands; DELETE FROM files;
                    DELETE FROM search;""")
                self._set_meta(conn, "config", self.cfg.fingerprint())
            casts, panes, cmdlog = self._scan_files()
            changed = self._sync_sessions(conn, casts)
            cmd_fp = self._fp(cmdlog) if cmdlog else None
            if changed or self._meta(conn, "commands_fp") != json.dumps(cmd_fp):
                self._sync_commands(conn, cmdlog)
                self._set_meta(conn, "commands_fp", json.dumps(cmd_fp))
            self._sync_panes(conn, panes)
            conn.commit()
            todo = conn.execute("SELECT count(*) FROM sessions WHERE outputs_done=0").fetchone()[0]
            total = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
            self._set_status(phase="ready" if todo == 0 else "pending", total=total,
                             done=total - todo, has_commands_log=cmdlog is not None)
        finally:
            conn.close()
        self.build_seconds = time.time() - t0

    @staticmethod
    def _fp(p: Path):
        st = p.stat()
        return [st.st_mtime, st.st_size]

    def _sync_sessions(self, conn, casts) -> bool:
        known = {r["file"]: r for r in conn.execute("SELECT id, file, mtime, size FROM sessions")}
        seen = set()
        changed = False
        for p, header in casts:
            rel = self.rel(p)
            seen.add(rel)
            st = p.stat()
            old = known.get(rel)
            if old and old["mtime"] == st.st_mtime and old["size"] == st.st_size:
                continue
            changed = True
            info = ac.scan(p)
            sid = session_id_for(rel)
            start = header.timestamp if header.timestamp is not None else st.st_mtime - info.duration
            self._drop_session_rows(conn, sid)
            conn.execute(
                "INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (sid, rel, header.version, start, info.duration, header.cols, header.rows,
                 info.n_events, header.idle_time_limit, json.dumps(info.markers),
                 st.st_mtime, st.st_size))
        for rel, row in known.items():
            if rel not in seen:
                changed = True
                self._drop_session_rows(conn, row["id"])
                conn.execute("DELETE FROM sessions WHERE id=?", (row["id"],))
        return changed

    def _drop_session_rows(self, conn, sid):
        conn.execute("DELETE FROM search WHERE session_id=? AND kind IN ('output', 'cmd')", (sid,))
        conn.execute("DELETE FROM commands WHERE session_id=? AND source='heuristic'", (sid,))

    def _sync_commands(self, conn, cmdlog: Path | None) -> None:
        conn.execute("DELETE FROM commands WHERE source='commands.log'")
        if cmdlog is not None:
            spans = [cm.Span(r["id"], r["start"], r["start"] + r["duration"])
                     for r in conn.execute("SELECT id, start, duration FROM sessions")]
            cmds = cm.attach(cm.parse_commands_log(cmdlog, self.tz), spans)
            conn.executemany(
                "INSERT INTO commands VALUES(?,?,?,?,?,?)",
                [(c.session_id, c.offset, c.abs_time, c.ctx, self._r(c.cmd), c.source) for c in cmds])
        self._rebuild_cmd_fts(conn)

    def _rebuild_cmd_fts(self, conn, session_id: str | None = None) -> None:
        """Les commandes indexées pour la recherche : celles de commands.log,
        ou à défaut (session sans commands.log) les heuristiques."""
        if session_id is None:
            conn.execute("DELETE FROM search WHERE kind='cmd'")
            where, args = "", ()
        else:
            conn.execute("DELETE FROM search WHERE kind='cmd' AND session_id=?", (session_id,))
            where, args = "AND c.session_id=?", (session_id,)
        conn.execute(f"""
            INSERT INTO search(text, kind, session_id, offset, abs_time, ref)
            SELECT c.cmd, 'cmd', c.session_id, c.offset, c.abs_time, c.source
            FROM commands c
            WHERE (c.source='commands.log' OR NOT EXISTS (
                SELECT 1 FROM commands l WHERE l.session_id=c.session_id
                AND l.source='commands.log')) {where}""", args)

    def _sync_panes(self, conn, panes: list[Path]) -> None:
        known = {r["path"]: r for r in conn.execute("SELECT * FROM files WHERE kind='pane'")}
        current = set()
        for p in panes:
            rel = self.rel(p)
            current.add(rel)
            st = p.stat()
            old = known.get(rel)
            if old and old["mtime"] == st.st_mtime and old["size"] == st.st_size:
                continue
            conn.execute("DELETE FROM search WHERE kind='pane' AND ref=?", (rel,))
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                chunk: list[str] = []
                for line in f:
                    chunk.append(line.rstrip("\r\n"))
                    if len(chunk) >= PANE_CHUNK_LINES:
                        self._insert_pane(conn, rel, chunk)
                        chunk = []
                self._insert_pane(conn, rel, chunk)
            conn.execute("INSERT OR REPLACE INTO files VALUES(?, 'pane', ?, ?)",
                         (rel, st.st_mtime, st.st_size))
        for rel in known:
            if rel not in current:
                conn.execute("DELETE FROM search WHERE kind='pane' AND ref=?", (rel,))
                conn.execute("DELETE FROM files WHERE path=?", (rel,))

    def _insert_pane(self, conn, rel, lines):
        text = "\n".join(l for l in lines if l.strip())
        if text:
            conn.execute("INSERT INTO search(text, kind, ref) VALUES(?, 'pane', ?)",
                         (self._r(text), rel))

    # -- phase de fond : sorties -------------------------------------------

    def start_background(self) -> None:
        if self._thread is not None or not terminal.available():
            if not terminal.available():
                self._set_status(phase="ready", error="pyte absent : recherche dans les sorties désactivée")
            return
        self._thread = threading.Thread(target=self._index_outputs, name="castdeck-index", daemon=True)
        self._thread.start()

    def _index_outputs(self) -> None:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE outputs_done=0 ORDER BY start").fetchall()
            total = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
            done = total - len(rows)
            self._set_status(phase="indexing" if rows else "ready", total=total, done=done)
            for row in rows:
                self._set_status(current=row["file"])
                try:
                    self._index_session_outputs(conn, row)
                except Exception as e:  # une session corrompue ne bloque pas les autres
                    conn.rollback()
                    print(f"castdeck: indexation de {row['file']} : {e}", file=sys.stderr)
                    conn.execute("UPDATE sessions SET outputs_done=1 WHERE id=?", (row["id"],))
                    conn.commit()
                done += 1
                self._set_status(done=done)
            self._compact(conn)
            self._set_status(phase="ready", current=None)
        except Exception as e:
            self._set_status(phase="error", error=str(e))
        finally:
            conn.close()

    def _index_session_outputs(self, conn, row) -> None:
        sid = row["id"]
        log_offsets = [r[0] for r in conn.execute(
            "SELECT offset FROM commands WHERE session_id=? AND source='commands.log'", (sid,))]
        start = row["start"]
        conn.execute("DELETE FROM search WHERE session_id=? AND kind='output'", (sid,))
        conn.execute("DELETE FROM commands WHERE session_id=? AND source='heuristic'", (sid,))
        batch: list[tuple] = []

        def flush():
            conn.executemany(
                "INSERT INTO search(text, kind, session_id, offset, abs_time) "
                "VALUES(?, 'output', ?, ?, ?)", batch)
            batch.clear()

        def on_snapshot(s: terminal.Snapshot):
            batch.append((self._r(s.text), sid, s.offset, start + s.offset))
            if len(batch) >= 200:
                flush()

        # tout dans une seule transaction : en cas d'erreur, rien n'est à moitié écrit
        _, detected = terminal.replay(
            self.root / row["file"], interval=self.cfg.snapshot_interval,
            extra_times=log_offsets, prompt_regex=self.cfg.prompt_regex,
            on_snapshot=on_snapshot)
        flush()
        conn.executemany(
            "INSERT INTO commands VALUES(?,?,?,?,?, 'heuristic')",
            [(sid, d.offset, start + d.offset, "cli", self._r(d.cmd)) for d in detected])
        self._rebuild_cmd_fts(conn, sid)
        conn.execute("UPDATE sessions SET outputs_done=1 WHERE id=?", (sid,))
        conn.commit()

    @staticmethod
    def _compact(conn) -> None:
        """SQLite ne rend pas la place libérée par les réindexations."""
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        total = conn.execute("PRAGMA page_count").fetchone()[0]
        if total and free / total > 0.25:
            conn.execute("VACUUM")

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # -- requêtes ----------------------------------------------------------

    def sessions(self, conn) -> list[dict]:
        rows = conn.execute("""
            SELECT s.*, (
              SELECT count(*) FROM commands c WHERE c.session_id=s.id AND (
                c.source='commands.log' OR NOT EXISTS (SELECT 1 FROM commands l
                WHERE l.session_id=s.id AND l.source='commands.log'))) AS n_commands
            FROM sessions s ORDER BY start""").fetchall()
        return [self._session_dict(r) for r in rows]

    def session(self, conn, sid: str) -> dict | None:
        for s in self.sessions(conn):
            if s["id"] == sid:
                return s
        return None

    @staticmethod
    def _session_dict(r) -> dict:
        return {
            "id": r["id"], "file": r["file"], "version": r["version"],
            "start": r["start"],
            "start_iso": datetime.fromtimestamp(r["start"], timezone.utc).isoformat(),
            "duration": r["duration"], "cols": r["cols"], "rows": r["rows"],
            "n_events": r["n_events"], "n_commands": r["n_commands"],
            "idle_time_limit": r["idle_time_limit"],
            "markers": json.loads(r["markers"] or "[]"),
            "outputs_indexed": bool(r["outputs_done"]),
        }

    def commands(self, conn, sid: str) -> list[dict]:
        has_log = conn.execute(
            "SELECT 1 FROM commands WHERE session_id=? AND source='commands.log' LIMIT 1",
            (sid,)).fetchone()
        source = "commands.log" if has_log else "heuristic"
        rows = conn.execute(
            "SELECT offset, abs_time, ctx, cmd, source FROM commands "
            "WHERE session_id=? AND source=? ORDER BY offset", (sid, source)).fetchall()
        return [dict(r) for r in rows]

    def search(self, conn, q: str, session: str | None = None, kind: str = "all",
               t_from: float | None = None, t_to: float | None = None,
               limit: int = 1000) -> list[dict]:
        terms = [a or b for a, b in re.findall(r'"([^"]+)"|(\S+)', q or "")]
        terms = [t for t in terms if t.strip()]
        if not terms:
            return []
        where, args = [], []
        use_fts = self.tokenizer == "trigram" and all(len(t) >= 3 for t in terms)
        if use_fts:
            where.append("search MATCH ?")
            args.append(" ".join('"' + t.replace('"', '""') + '"' for t in terms))
        elif self.tokenizer != "trigram" and terms:
            where.append("search MATCH ?")
            args.append(" ".join('"' + t.replace('"', '""') + '"*' for t in terms))
        else:
            for t in terms:
                where.append(r"text LIKE ? ESCAPE '\'")
                args.append("%" + re.sub(r"([%_\\])", r"\\\1", t) + "%")
        kinds = {"cmd": ("cmd",), "output": ("output", "pane"), "all": ("cmd", "output", "pane")}
        ks = kinds.get(kind, kinds["all"])
        where.append(f"kind IN ({','.join('?' * len(ks))})")
        args.extend(ks)
        if session:
            where.append("session_id=?")
            args.append(session)
        if t_from is not None or t_to is not None:
            where.append("abs_time IS NOT NULL")
        if t_from is not None:
            where.append("CAST(abs_time AS REAL) >= ?")
            args.append(t_from)
        if t_to is not None:
            where.append("CAST(abs_time AS REAL) <= ?")
            args.append(t_to)
        sql = (f"SELECT text, kind, session_id, offset, abs_time, ref FROM search "
               f"WHERE {' AND '.join(where)} "
               f"ORDER BY CASE kind WHEN 'cmd' THEN 0 WHEN 'output' THEN 1 ELSE 2 END, "
               f"CAST(abs_time AS REAL) LIMIT ?")
        args.append(limit * 3)
        out, seen = [], set()
        for r in conn.execute(sql, args):
            snippet = make_snippet(r["text"], terms, whole=r["kind"] == "cmd")
            # sorties : un même texte revu plus tard est un doublon ; commandes :
            # la même commande retapée à un autre moment est un résultat distinct
            when = r["offset"] if r["kind"] == "cmd" else None
            key = (r["session_id"], r["kind"], r["ref"] if r["kind"] == "pane" else None, when, snippet)
            if key in seen:
                continue
            seen.add(key)
            item = {
                "session_id": r["session_id"],
                "offset": float(r["offset"]) if r["offset"] is not None else None,
                "abs_time": float(r["abs_time"]) if r["abs_time"] is not None else None,
                "kind": r["kind"],
                "snippet": snippet,
            }
            if r["kind"] == "pane":
                item["pane"] = r["ref"]
            elif r["kind"] == "cmd":
                item["source"] = r["ref"]
            out.append(item)
            if len(out) >= limit:
                break
        out.sort(key=lambda i: ({"cmd": 0, "output": 1, "pane": 2}[i["kind"]],
                                i["abs_time"] if i["abs_time"] is not None else float("inf")))
        return out


def make_snippet(text: str, terms: list[str], whole: bool = False, max_lines: int = 3,
                 width: int = 160) -> str:
    """HTML échappé avec <mark> autour des termes. Pour une sortie : seulement
    les lignes qui matchent (max `max_lines`)."""
    lowered = [t.lower() for t in terms]
    lines = text.split("\n") if not whole else [text]
    picked = [l for l in lines if any(t in l.lower() for t in lowered)][:max_lines]
    if not picked:
        picked = lines[:1]
    out = []
    for line in picked:
        if not whole and len(line) > width:
            pos = min((line.lower().find(t) for t in lowered if t in line.lower()), default=0)
            lo = max(0, pos - width // 3)
            line = ("…" if lo else "") + line[lo:lo + width] + ("…" if lo + width < len(line) else "")
        out.append(_highlight(line, lowered))
    return "\n".join(out)


def _highlight(line: str, lowered: list[str]) -> str:
    low = line.lower()
    marks = [False] * len(line)
    for t in lowered:
        start = 0
        while t and (i := low.find(t, start)) >= 0:
            for k in range(i, i + len(t)):
                marks[k] = True
            start = i + 1
    out, i = [], 0
    while i < len(line):
        j = i
        while j < len(line) and marks[j] == marks[i]:
            j += 1
        seg = html.escape(line[i:j])
        out.append(f"<mark>{seg}</mark>" if marks[i] else seg)
        i = j
    return "".join(out)
