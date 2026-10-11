# Sécurité

castdeck manipule des **logs de mission** : commandes, sorties, parfois des identifiants ou des hash.

## Ce que fait castdeck

- Il écoute sur `127.0.0.1` par défaut. Écouter ailleurs demande `--host` explicitement, et castdeck affiche alors un avertissement.
- Il refuse les requêtes dont les en-têtes `Host` ou `Origin` ne correspondent pas à l'adresse locale (protection contre le DNS rebinding).
- Il n'envoie aucune télémétrie et ne charge aucun asset distant. La CSP bloque toute requête sortante du navigateur.
- Il ne modifie jamais les fichiers de logs.

## Ce que tu dois savoir

- `<dossier>/.castdeck/index.db` contient le texte des commandes et des sorties : traite-le comme les logs eux-mêmes.
- Ne versionne jamais tes logs. Le `.gitignore` de ce repo exclut les `*.asciinema`, `*.cast`, `commands.log` et `panes_*.log` placés à la racine.
- `--redact` masque les secrets courants dans l'index et l'UI. Dans le flux vidéo, c'est du *best effort*.

## Signaler une vulnérabilité

Merci d'ouvrir une *security advisory* privée sur GitHub (onglet **Security**, puis **Report a vulnerability**)
plutôt qu'une issue publique.
