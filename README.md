# IPTV Master

Petit outil GUI (PySide6) pour charger des playlists M3U, tester les URLs, exporter des listes filtrées et consommer un guide EPG (XMLTV).

## Prérequis
- Python 3.10+
- VLC installé sur la machine (pour python-vlc)
- Optionnel : Node.js + npm si tu génères l'EPG via le dossier epg/

## Installation rapide
`ash
python -m venv .venv
./.venv/Scripts/activate   # Windows (ou source .venv/bin/activate sous Linux/macOS)
pip install -r requirements.txt
`

## Lancer l'application
`ash
python app.py
`
- La base locale SQLite est stockée dans data/iptv.db.
- Les playlists importées peuvent être testées, filtrées et exportées depuis l'UI.

## EPG (XMLTV)
- Fournis un chemin/URL XMLTV dans le champ EPG de l'UI, ou pointe vers le dépôt npm epg/ (il contient un package.json).
- Quand un tvg-id est sélectionné et que l'EPG est chargé, le bouton « Guide » ouvre le détail des programmes.

## Tests
Des scripts et tests de debug se trouvent dans TESTS/ (ex : TESTS/test_vlc.py, TESTS/test_parsing.py).

## Divers
- Les artefacts locaux (DB, archives, node_modules, etc.) sont ignorés via .gitignore.
- Le dépôt epg/ possède son propre .gitignore pour les assets npm/temp.