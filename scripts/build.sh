#!/usr/bin/env bash
# Build complet : front (Vite) -> castdeck/static, puis binaire autonome
# dist/castdeck (zipapp Python avec pyte embarqué, aucune dépendance à installer).
# PYTHON=/chemin/python pour choisir l'interpréteur du build (il lui faut pip).
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${SKIP_FRONT:-0}" != 1 ]]; then
  (cd frontend && npm ci --no-audit --no-fund && npm run build)
fi
test -f castdeck/static/index.html || { echo "front non buildé" >&2; exit 1; }

PY="${PYTHON:-python3}"
rm -rf build/zipapp dist/castdeck
mkdir -p build/zipapp dist
"$PY" -m pip install --quiet --no-compile --target build/zipapp .
rm -rf build/zipapp/*.dist-info build/zipapp/bin
"$PY" -m zipapp build/zipapp -m "castdeck.cli:main" -p "/usr/bin/env python3" -c -o dist/castdeck
echo "-> dist/castdeck ($(du -h dist/castdeck | cut -f1))"
