"""Serveur HTTP (stdlib) : API JSON + front statique.

Aucune ressource distante : la CSP n'autorise que 'self' (+ wasm-unsafe-eval pour le
module WebAssembly embarqué du player), ce qui rend toute
requête sortante du navigateur impossible.
"""
from __future__ import annotations

import json
import mimetypes
import re
import sys
import threading
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from . import __version__
from . import asciicast as ac
from . import terminal
from .bookmarks import BookmarkStore
from .index import Index
from .redact import redact

CSP = ("default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
       "worker-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
       "form-action 'none'")

_SESSION_ROUTE = re.compile(r"^/api/sessions/([0-9a-f]{12})/(cast|commands|bookmarks|timemap|screen)$")


class App:
    def __init__(self, index: Index, host: str, port: int, allow_any_host: bool = False):
        self.index = index
        self.host = host
        self.port = port
        self.allow_any_host = allow_any_host
        self.bookmarks = BookmarkStore(index.data_dir / "bookmarks.json")
        self._local = threading.local()

    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = self.index.connect()
        return c

    def allowed_host(self, host_header: str | None) -> bool:
        if self.allow_any_host:
            return True
        if not host_header:
            return False
        ok = {f"{h}:{self.port}" for h in ("127.0.0.1", "localhost", "[::1]")}
        return host_header.lower() in ok

    @lru_cache(maxsize=64)
    def timemap(self, sid: str, mtime: float, limit: float) -> dict:
        path = self.index.root / self.session_file(sid)
        times = [t for t, _, _ in ac.iter_events(path)]
        knots = ac.time_knots(times, limit)
        duration = times[-1] if times else 0.0
        return {"limit": limit, "knots": knots,
                "display_duration": ac.orig_to_display(knots, duration)}

    def session_row(self, sid: str):
        return self.conn().execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()

    def session_file(self, sid: str) -> str:
        return self.session_row(sid)["file"]


def _static_root():
    return resources.files("castdeck") / "static"


class Handler(BaseHTTPRequestHandler):
    server_version = f"castdeck/{__version__}"
    app: App  # injecté par make_server

    def log_message(self, fmt, *args):  # silencieux sauf erreurs
        pass

    # -- utilitaires -------------------------------------------------------

    def _headers(self, status=200, ctype="application/json; charset=utf-8", length=None,
                 cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self._headers(status, length=len(body))
        self.wfile.write(body)

    def _error(self, status: HTTPStatus, msg: str):
        self._json({"error": msg}, status)

    def _guard(self) -> bool:
        if not self.app.allowed_host(self.headers.get("Host")):
            self._error(HTTPStatus.FORBIDDEN, "Host non autorisé")
            return False
        return True

    # -- verbes ------------------------------------------------------------

    def do_GET(self):
        if not self._guard():
            return
        url = urlparse(self.path)
        qs = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if url.path.startswith("/api/"):
                self._api_get(url.path, qs)
            else:
                self._static(url.path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError as e:
            self._error(HTTPStatus.BAD_REQUEST, str(e))
        except Exception as e:  # pragma: no cover
            print(f"castdeck: erreur {url.path} : {e!r}", file=sys.stderr)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "erreur interne")

    def do_PUT(self):
        if not self._guard():
            return
        origin = self.headers.get("Origin")
        if origin and not self.app.allowed_host(urlparse(origin).netloc):
            return self._error(HTTPStatus.FORBIDDEN, "Origin non autorisée")
        m = _SESSION_ROUTE.match(urlparse(self.path).path)
        if not m or m.group(2) != "bookmarks":
            return self._error(HTTPStatus.NOT_FOUND, "introuvable")
        row = self.app.session_row(m.group(1))
        if row is None:
            return self._error(HTTPStatus.NOT_FOUND, "session inconnue")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                raise ValueError("trop gros")
            items = json.loads(self.rfile.read(length) or b"[]")
            self._json(self.app.bookmarks.put(row["file"], items))
        except ValueError as e:
            self._error(HTTPStatus.BAD_REQUEST, str(e))

    # -- API ---------------------------------------------------------------

    def _api_get(self, path: str, qs: dict):
        app, idx = self.app, self.app.index
        if path == "/api/config":
            return self._json({
                "version": __version__, "tz": idx.cfg.tz, "redact": idx.cfg.redact,
                "idle_limit": idx.cfg.idle_limit, "root": str(idx.root),
                "outputs_available": terminal.available(),
            })
        if path == "/api/status":
            return self._json(idx.get_status())
        if path == "/api/sessions":
            return self._json(idx.sessions(app.conn()))
        if path == "/api/search":
            return self._json(idx.search(
                app.conn(), qs.get("q", ""), session=qs.get("session") or None,
                kind=qs.get("kind", "all"),
                t_from=_float(qs.get("from")), t_to=_float(qs.get("to")),
                limit=min(int(qs.get("limit", 1000)), 5000)))
        m = _SESSION_ROUTE.match(path)
        if not m:
            return self._error(HTTPStatus.NOT_FOUND, "introuvable")
        sid, what = m.groups()
        row = app.session_row(sid)
        if row is None:
            return self._error(HTTPStatus.NOT_FOUND, "session inconnue")
        if what == "commands":
            return self._json(idx.commands(app.conn(), sid))
        if what == "bookmarks":
            return self._json(app.bookmarks.get(row["file"]))
        if what == "timemap":
            limit = _float(qs.get("idle")) or idx.cfg.idle_limit
            return self._json(app.timemap(sid, row["mtime"], limit))
        if what == "screen":
            if not terminal.available():
                return self._error(HTTPStatus.NOT_IMPLEMENTED, "pyte absent")
            text = terminal.screen_at(idx.root / row["file"], _float(qs.get("t")) or 0.0)
            return self._json({"text": redact(text) if idx.cfg.redact else text})
        if what == "cast":
            idle = _float(qs.get("idle"))
            lines = ac.to_v2_lines(idx.root / row["file"], idle_limit=idle,
                                   transform=redact if idx.cfg.redact else None)
            self._headers(ctype="application/x-asciicast; charset=utf-8")
            buf = []
            size = 0
            for line in lines:
                b = line.encode()
                buf.append(b)
                size += len(b)
                if size > 256 * 1024:
                    self.wfile.write(b"".join(buf))
                    buf, size = [], 0
            self.wfile.write(b"".join(buf))
            return

    # -- statique ----------------------------------------------------------

    def _static(self, path: str):
        root = _static_root()
        rel = path.lstrip("/") or "index.html"
        if ".." in rel.split("/"):
            return self._error(HTTPStatus.NOT_FOUND, "introuvable")
        node = root
        for part in rel.split("/"):
            node = node / part
        if not node.is_file():
            node = root / "index.html"  # SPA
            rel = "index.html"
            if not node.is_file():
                body = b"Front non builde : lancez `npm run build` dans frontend/."
                self._headers(500, "text/plain; charset=utf-8", len(body))
                self.wfile.write(body)
                return
        body = node.read_bytes()
        ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        cache = "public, max-age=31536000, immutable" if rel.startswith("assets/") else "no-cache"
        self._headers(200, ctype, len(body), cache)
        self.wfile.write(body)


def _float(v):
    if v in (None, ""):
        return None
    return float(v)


def make_server(index: Index, host: str = "127.0.0.1", port: int = 8000,
                allow_any_host: bool = False) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    real_port = httpd.server_address[1]
    app = App(index, host, real_port, allow_any_host)
    httpd.RequestHandlerClass = type("BoundHandler", (Handler,), {"app": app})
    httpd.daemon_threads = True
    return httpd
