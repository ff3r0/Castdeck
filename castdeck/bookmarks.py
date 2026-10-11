"""Bookmarks utilisateur, stockés dans .castdeck/bookmarks.json (jamais dans
les fichiers .asciinema). Clé = chemin relatif du fichier de session."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class BookmarkStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, file: str) -> list[dict]:
        with self._lock:
            return self._load().get(file, [])

    def put(self, file: str, items: list) -> list[dict]:
        clean = validate(items)
        with self._lock:
            data = self._load()
            if clean:
                data[file] = clean
            else:
                data.pop(file, None)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        return clean


def validate(items) -> list[dict]:
    if not isinstance(items, list):
        raise ValueError("liste attendue")
    out = []
    for it in items[:1000]:
        if not isinstance(it, dict):
            raise ValueError("bookmark invalide")
        offset = it.get("offset")
        if not isinstance(offset, (int, float)) or offset < 0:
            raise ValueError("offset invalide")
        label = str(it.get("label") or "")[:200]
        created = it.get("created")
        out.append({
            "offset": float(offset),
            "label": label,
            "created": created if isinstance(created, (int, float)) else time.time(),
        })
    out.sort(key=lambda b: b["offset"])
    return out
