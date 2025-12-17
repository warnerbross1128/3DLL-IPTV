# IPTV Master

Petit outil GUI (PySide6) pour charger des playlists M3U, tester les URLs, exporter des listes filtr?es et consommer un guide EPG (XMLTV).

## Pr?requis
- Python 3.10+
- VLC install? sur la machine (pour python-vlc)
- Optionnel : Node.js + npm si tu g?n?res l'EPG via le dossier epg/

## Installation rapide
```bash
python -m venv .venv
./.venv/Scripts/activate   # Windows (ou source .venv/bin/activate sous Linux/macOS)
pip install -r requirements.txt
```

## Lancer l'application
```bash
python app.py
```
- La base locale SQLite est stock?e dans data/iptv.db.
- Les playlists import?es peuvent ?tre test?es, filtr?es et export?es depuis l'UI.

## Scoring indicatif des flux
- Chaque cha?ne re?oit un score 0-100 + badge (🟢/🟡/🔴) en fonction de signaux URL/domaine/mots-cl?s/coh?rence g?o.
- Le score est affich? dans le tableau comme information; aucune d?cision l?gale/technique n'est prise automatiquement.

## EPG (XMLTV)
- Fournis un chemin/URL XMLTV dans le champ EPG de l'UI, ou pointe vers le d?p?t npm epg/ (il contient un package.json).
- Quand un tvg-id est s?lectionn? et que l'EPG est charg?, le bouton ? Guide ? ouvre le d?tail des programmes.

## Tests
Des scripts et tests de debug se trouvent dans TESTS/ (ex : TESTS/test_vlc.py, TESTS/test_parsing.py).

## Divers
- Les artefacts locaux (DB, archives, node_modules, etc.) sont ignor?s via .gitignore.
- Le d?p?t epg/ poss?de son propre .gitignore pour les assets npm/temp.
