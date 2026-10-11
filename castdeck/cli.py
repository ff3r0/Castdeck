"""castdeck /workspace/logs [--port 9000] [--reindex] ..."""
from __future__ import annotations

import argparse
import sys
import time
import webbrowser
from pathlib import Path

from . import __version__, terminal
from .index import Config, Index
from .server import make_server


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="castdeck",
        description="Visionneuse locale de sessions asciinema (exegol --log).")
    p.add_argument("folder", type=Path, help="dossier des logs (.asciinema / .cast, commands.log, panes_*.log)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default=None,
                   help="adresse d'écoute (défaut 127.0.0.1). Exposer ailleurs = exposer les logs de mission.")
    p.add_argument("--reindex", action="store_true", help="reconstruit l'index depuis zéro")
    p.add_argument("--tz", default=None,
                   help="fuseau de commands.log (ex. Europe/Paris). Défaut : fuseau système")
    p.add_argument("--redact", action="store_true",
                   help="masque les secrets courants (-p, password=, hash NTLM, JWT) dans l'UI et l'index")
    p.add_argument("--prompt-regex", default=terminal.DEFAULT_PROMPT_REGEX,
                   help="regex du prompt pour détecter les commandes sans commands.log "
                        "(groupe 1 = la commande). Défaut : %(default)s")
    p.add_argument("--snapshot-interval", type=float, default=5.0,
                   help="secondes entre deux snapshots d'écran indexés (défaut 5)")
    p.add_argument("--idle-limit", type=float, default=2.0,
                   help="durée max d'un temps mort en mode 'compresser' (défaut 2 s)")
    p.add_argument("--index-dir", type=Path, default=None,
                   help="emplacement de l'index (défaut <folder>/.castdeck)")
    p.add_argument("--index-only", action="store_true", help="indexe puis quitte (sans serveur)")
    p.add_argument("--open", action="store_true", help="ouvre le navigateur")
    p.add_argument("--version", action="version", version=f"castdeck {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    folder: Path = args.folder.expanduser()
    if not folder.is_dir():
        print(f"castdeck: {folder} n'est pas un dossier", file=sys.stderr)
        return 2
    if args.index_dir:
        args.index_dir.mkdir(parents=True, exist_ok=True)

    cfg = Config(root=folder, data_dir=args.index_dir, tz=args.tz, redact=args.redact,
                 prompt_regex=args.prompt_regex or None,
                 snapshot_interval=max(args.snapshot_interval, 0.5),
                 idle_limit=args.idle_limit)
    index = Index(cfg)
    index.build(force=args.reindex)
    st = index.get_status()
    print(f"castdeck: {st['total']} session(s) indexée(s) en {index.build_seconds:.2f}s "
          f"— index : {index.db_path}")
    if not terminal.available():
        print("castdeck: pyte absent -> pas de recherche dans les sorties "
              "(pip install pyte)", file=sys.stderr)

    if args.index_only:
        index.start_background()
        index.wait()
        print("castdeck: index des sorties terminé")
        return 0

    host = args.host or "127.0.0.1"
    if args.host and args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"castdeck: ATTENTION écoute sur {args.host} : les logs de mission (et "
              "potentiellement des secrets) seront accessibles depuis le réseau.", file=sys.stderr)
    try:
        httpd = make_server(index, host, args.port, allow_any_host=args.host is not None)
    except OSError as e:
        print(f"castdeck: impossible d'écouter sur {host}:{args.port} : {e}", file=sys.stderr)
        return 1
    index.start_background()
    shown = f"[{host}]" if ":" in host else host
    url = f"http://{shown}:{httpd.server_address[1]}"
    print(f"castdeck: {url}  (Ctrl+C pour quitter)")
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
