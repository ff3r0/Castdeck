#!/usr/bin/env bash
# Dépose dist/castdeck dans ~/.exegol/my-resources/bin/ : il est alors dans le
# PATH de tous les conteneurs Exegol (/opt/my-resources/bin).
set -euo pipefail
cd "$(dirname "$0")/.."
DEST="${EXEGOL_MY_RESOURCES:-$HOME/.exegol/my-resources}/bin"
[[ -f dist/castdeck ]] || ./scripts/build.sh
mkdir -p "$DEST"
install -m 0755 dist/castdeck "$DEST/castdeck"
echo "castdeck installé dans $DEST/castdeck"
echo "Dans un conteneur : castdeck /workspace/logs   (ou le dossier passé à exegol --log)"
