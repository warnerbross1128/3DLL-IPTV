# IPTV Master

Application de bureau PySide6 pour naviguer dans les playlists IPTV, filtrer/assainir les flux, tester leur accessibilite, consommer un guide EPG et lire les chaines via VLC.

## Fonctionnalites principales
- **Browser GitHub** : telecharge `PLAYLISTS.md` du repo iptv-org/iptv, classe par Category/Language/Country/City, filtre par texte et importe une selection (fusion automatique des M3U).
- **Editeur de playlist** : import M3U depuis fichier ou URL, recherche instantanee (nom/groupe/tvg-id/url), suppression rapide (KO ou selection), export M3U filtre.
- **Scoring de risque** : heuristique offline 0-100 avec badge (Faible/Modere/Eleve) base sur URL, TLD, IP brute, mots-cles et coherence geo; resume log et tooltip par ligne.
- **Test des URLs** : probe parallele en QThread + sous-processus (HEAD/GET) avec timeouts courts; statut par ligne et bouton Stop.
- **EPG (XMLTV)** : telechargement (XML ou .gz), parsing stream et insertion SQLite; affichage now/next, bouton Guide pour un tvg-id, et EPG embarque dans le lecteur.
- **Pont npm (iptv-org/epg)** : si le champ EPG pointe vers un repo avec `package.json`, genere un XMLTV cible pour les tvg-id charges via `npm run grab` puis merge.
- **Lecteur VLC integre** : panneau avec playlist filtrable, now/next et guide EPG repliables; double-clic pour lire une chaine depuis l'onglet Editeur ou le Salon.
- **Salon (playlists locales)** : stocke playlists dans SQLite, recherche, suppression, chargement direct dans le lecteur ou ouverture dans l'editeur.
- **Theming** : palettes `ui/themes/*.py` chargees dynamiquement (light/dark/ocean/forest/sunset/retro, etc.) + choix du style Qt; preference persistee dans `data/config.json`.
- **Log repliable** : panneau basculable pour suivre telechargements, probes, EPG, actions DB.

## Architecture rapide
- `app.py` : point d'entree, QApplication + MainWindow.
- `ui/main_window.py` : conteneur principal, onglets UI, logique de telechargement, fusion, probes, EPG et wiring VLC.
- `core/m3u.py` : parsing/ecriture M3U minimal.
- `core/risk_scoring.py` : heuristique de scoring + mutations sur les objets `Channel`.
- `storage.py` : persistence SQLite (playlists, channels, epg_sources, epg_programs) et requetes now/next.
- `epg_xmltv.py` : download/decompress XMLTV et parsing incremental.
- `epg_npm_bridge.py` : scan des sites iptv-org/epg, generation de channels filtres, execution `npm run grab` et merge XMLTV.
- `imbed_vlc.py` : widgets VLC (player, playlist, now/next, guide) + sections repliables.
- `workers/probe_worker.py` : probe HTTP en sous-processus, reporte dans Qt.
- `salon_tab.py`, `ui/settings_tab.py`, `ui/themes/` : onglet Salon, preferences et theming.
- `TESTS/` : scripts de debug et tests unitaires (parsing, risk scoring, VLC, EPG bridge).

## Prerequis
- Python 3.10+.
- VLC installe (python-vlc utilise la lib VLC disponible sur le systeme).
- Node.js + npm (optionnel, requis uniquement pour la generation EPG via `epg/`).
- Acces reseau pour telecharger playlists/EPG ou lancer les probes HTTP.

## Installation
```bash
python -m venv .venv
# Windows
.\.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
```
Pour l'EPG npm, aller dans `epg/` et executer `npm install` une fois.

## Lancement
```bash
python app.py
```
La base locale SQLite est creee dans `data/iptv.db` (auto, pas de config manuelle).

## Guide d'utilisation (UI)
1) **Playlists (GitHub)** : clic sur "Lister playlists", filtrer, selectionner un ou plusieurs noeuds (categorie/langue/pays/ville) puis "Charger la selection".
2) **Editeur de chaines** : importer M3U (fichier ou URL), filtrer dans le champ de recherche, lancer "Tester URLs", supprimer KO ou la selection, exporter M3U ou "Exporter au Salon".
3) **EPG** : saisir une URL XMLTV (.xml ou .gz) ou un dossier `epg/` contenant `package.json`, cliquer "Mettre a jour EPG", puis "Guide" sur une chaine avec tvg-id. Now/Next se met a jour au clic.
4) **Lecteur** : onglet "Lecteur", playlist filtrable et double-clic pour lire. Now/Next + Guide charges si l'EPG est importe.
5) **Salon** : playlists sauvegardees en DB; rechercher, double-clic pour charger dans le lecteur ou bouton pour ouvrir dans l'editeur; suppression disponible via clic droit ou bouton.
6) **Configuration** : choisir theme Qt et style; preference persistee dans `data/config.json`. Bouton "Log" replie/deroule le journal.

## EPG via iptv-org/epg (npm)
- Cloner/extraire le repo iptv-org/epg dans `epg/` (ou pointer le champ EPG vers son chemin absolu).
- Lancer `npm install` dans ce dossier.
- Charger une playlist avec des tvg-id; cliquer "Mettre a jour EPG" avec le chemin du repo en cours. Le bridge selectionne les sites utiles, genere des `custom.channels.xml` et merge le XMLTV final.
- Note: beaucoup de `*.channels.xml` utilisent des `xmltv_id` avec suffixe `@SD/@HD` (ex: `France2.fr@SD`). Le bridge ignore ce suffixe pour le matching et réécrit `xmltv_id` dans les fichiers `custom.channels.xml` afin que les IDs du XMLTV généré correspondent aux `tvg-id` de ta playlist.
- Par défaut, le bridge réduit automatiquement le nombre de sites via une sélection "greedy" (max 12, configurable via `IPTV_EPG_MAX_SITES`) pour éviter des runs très longs.

## Tests et debug
- Exemple : `python -m pytest TESTS` (installer pytest si besoin).

## Notes
- Le scoring est informatif uniquement (aucune decision automatique).
- Les probes HTTP ouvrent des sous-processus `spawn` pour eviter les blocages; peut prendre quelques secondes selon le timeout choisi.
- Les assets volumineux (archives, node_modules, DB) sont ignores via `.gitignore`.
