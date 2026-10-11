# Changelog

## [0.1.0] - 2026-10-11

Première version.

### Ajouté
- Parsing streaming asciicast v2 et v3 (`.asciinema` / `.cast`, détection par le header).
- Serveur local (stdlib) avec API JSON et front Vite + TypeScript servi en statique, 100 % offline.
- Playlist multi-sessions, file de lecture par glisser-déposer, enchaînement automatique.
- Panneau des commandes depuis `commands.log` (clic pour sauter, copie, filtre `ctx=`), et détection heuristique par le prompt quand `commands.log` est absent.
- Recherche plein texte (SQLite FTS5 trigram) dans les commandes, les sorties écran (snapshots pyte) et les `panes_*.log`.
- Timeline enrichie, bookmarks (`.castdeck/bookmarks.json`), « aller à » en heure absolue d'une session à l'autre.
- Compression des temps morts avec conversion exacte des positions (piège `idleTimeLimit`).
- Captures PNG (fichier ou presse-papier) et copie du texte de l'écran.
- Raccourcis clavier, thème sombre type Exegol.
- Option `--redact`, protection Host/Origin, CSP stricte.
- Packaging `pipx` et binaire zipapp autonome pour `~/.exegol/my-resources/bin/`.
