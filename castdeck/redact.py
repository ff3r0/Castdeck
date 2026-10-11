"""Option --redact : masque les secrets courants.

On remplace par des ``*`` de MÊME longueur pour ne pas décaler l'affichage du
terminal. Best effort : dans le flux brut du player, un secret tapé caractère
par caractère (echo) arrive en plusieurs events et peut échapper au masquage ;
l'index et les listes de commandes, eux, sont masqués de façon fiable.
"""
from __future__ import annotations

import re

_VALUE = r"""('[^']*'|"[^"]*"|\S+)"""
_KV_VALUE = r"""('[^']*'|"[^"]*"|[^\s&;'"]+)"""

_PATTERNS = [
    # -p 'xxx' / -p xxx  (nxc, smbclient, impacket...)  -> valeur masquée
    re.compile(r"(?<![\w-])(-p\s+)" + _VALUE),
    re.compile(r"(--pass(?:word)?[=\s]+)" + _VALUE, re.I),
    re.compile(r"((?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key)\s*[=:]\s*)" + _KV_VALUE, re.I),
    # hash NTLM (LM:NT ou NT seul), 32 hex
    re.compile(r"()\b([0-9a-fA-F]{32}(?::[0-9a-fA-F]{32})?)\b"),
    # JWT
    re.compile(r"()\b(eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*)"),
]


def _mask(m: re.Match) -> str:
    return m.group(1) + "*" * len(m.group(2))


def redact(text: str) -> str:
    for p in _PATTERNS:
        text = p.sub(_mask, text)
    return text
