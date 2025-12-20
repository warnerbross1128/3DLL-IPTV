# ui/main_window.py
from __future__ import annotations

from collections import deque
import json
import re
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from PySide6 import QtCore, QtGui, QtWidgets

from core.models import Channel
from core.m3u import parse_m3u, write_m3u
from core.risk_scoring import score_channels
from workers.probe_worker import ProbeWorker

from imbed_vlc import VlcPlayerPanel
from storage import Storage
from epg_xmltv import download_xmltv, iter_programs
from epg_npm_bridge import generate_xmltv_for_tvg_ids
from salon_tab import SalonTab
from ui.settings_tab import SettingsTab
from ui.themes import discover_themes


# =========================
# Playlists index (iptv-org/api with fallback to PLAYLISTS.md)
# =========================

PLAYLISTS_API_BASE = "https://iptv-org.github.io/api"
IPTV_PLAYLIST_BASE = "https://iptv-org.github.io/iptv"
PLAYLISTS_MD_RAW = "https://raw.githubusercontent.com/iptv-org/iptv/master/PLAYLISTS.md"

CODE_URL_RE = re.compile(r"<code>\s*(https?://[^<\s]+?\.m3u8?)\s*</code>", re.IGNORECASE)
BT_URL_RE = re.compile(r"`(https?://[^`]+?\.m3u8?)`")
PLAIN_URL_RE = re.compile(r"^\s*(https?://\S+?\.m3u8?)\s*$")

TR_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.IGNORECASE)
TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(s: str) -> str:
    s = TAG_RE.sub("", s)
    return s.replace("&amp;", "&").replace("&nbsp;", " ").strip()


def _get_json(url: str, timeout: int):
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_feeds(timeout: int = 25) -> list[dict]:
    """
    Fetch iptv-org/api feeds list.
    Schema example:
      { channel, id, name, alt_names, is_main, broadcast_area, timezones, languages, format }
    """
    feeds = _get_json(f"{PLAYLISTS_API_BASE}/feeds.json", timeout)
    return feeds if isinstance(feeds, list) else []


def fetch_streams(timeout: int = 30) -> list[dict]:
    """
    Fetch iptv-org/api streams list.
    Schema example:
      { channel, feed, title, url, quality, user_agent, referrer }
    """
    streams = _get_json(f"{PLAYLISTS_API_BASE}/streams.json", timeout)
    return streams if isinstance(streams, list) else []


def build_m3u_from_api_streams(
    selected_feeds: list[dict],
    streams: list[dict],
    *,
    max_streams_per_feed: int = 6,
) -> str:
    """
    Build a simple M3U from selected feeds + streams.json.
    - Match streams primarily on (channel, feed-id), then fallback to (channel) if needed.
    - Adds EXTVLCOPT for referrer/user-agent when present.
    """
    out: list[str] = ["#EXTM3U"]

    by_chan_feed: dict[tuple[str, str], list[dict]] = {}
    by_chan: dict[str, list[dict]] = {}

    for s in streams or []:
        ch = (s.get("channel") or "").strip()
        fd = (s.get("feed") or "").strip()
        if not ch:
            continue
        by_chan.setdefault(ch, []).append(s)
        if fd:
            by_chan_feed.setdefault((ch, fd), []).append(s)

    for f in selected_feeds or []:
        channel_id = (f.get("channel") or "").strip()
        feed_id = (f.get("id") or "").strip()
        feed_name = (f.get("name") or "").strip() or channel_id or feed_id or "(feed)"

        group = ""
        ba = f.get("broadcast_area") or []
        if isinstance(ba, list) and ba:
            group = str(ba[0] or "").strip()

        candidates = []
        if channel_id and feed_id:
            candidates = list(by_chan_feed.get((channel_id, feed_id), []))
        if (not candidates) and channel_id:
            candidates = list(by_chan.get(channel_id, []))

        if not candidates:
            continue

        kept = 0
        for s in candidates:
            url = (s.get("url") or "").strip()
            if not url:
                continue

            title = (s.get("title") or "").strip() or feed_name
            quality = (s.get("quality") or "").strip()
            label = f"{title} [{quality}]" if quality else title

            extinf = f'#EXTINF:-1 tvg-id="{channel_id}"'
            if group:
                extinf += f' group-title="{group}"'
            extinf += f",{label}"
            out.append(extinf)

            ref = (s.get("referrer") or "").strip()
            ua = (s.get("user_agent") or "").strip()
            if ref:
                out.append(f"#EXTVLCOPT:http-referrer={ref}")
            if ua:
                out.append(f"#EXTVLCOPT:http-user-agent={ua}")

            out.append(url)
            kept += 1
            if kept >= int(max_streams_per_feed):
                break

    return "\n".join(out) + "\n"


def _bucket_from_api(timeout: int) -> dict:
    """
    Construit les playlists (categories/langues/pays/areas) en s'appuyant sur iptv-org/api,
    en generant les URLs previsibles du repo iptv (ex: /languages/fra.m3u).

    Important:
    - `languages.json` et `subdivisions.json` contiennent des milliers d'entrees (ISO/administratif).
      On filtree donc avec `feeds.json` pour ne garder que ce qui est utilise, sinon on produirait
      beaucoup d'URLs inexistantes cote iptv-org/iptv.
    """
    buckets = {"Category": [], "Language": [], "Country": [], "Subdivision/City": []}
    seen = set()

    def add(bucket: str, name: str, url: str):
        key = (bucket, name, url)
        if key in seen:
            return
        seen.add(key)
        buckets[bucket].append((name, url))

    # Feeds: source de verite pour "ce qui existe vraiment" dans l'ecosysteme iptv.
    feeds = _get_json(f"{PLAYLISTS_API_BASE}/feeds.json", timeout)

    used_languages: set[str] = set()
    used_countries: set[str] = set()
    used_subdivisions: set[str] = set()
    used_cities: set[str] = set()

    for f in feeds or []:
        for lang in (f.get("languages") or []):
            if isinstance(lang, str) and lang.strip():
                used_languages.add(lang.strip())

        for area in (f.get("broadcast_area") or []):
            if not isinstance(area, str):
                continue
            area = area.strip()
            if area.startswith("c/") and len(area) > 2:
                used_countries.add(area[2:])
            elif area.startswith("s/") and len(area) > 2:
                used_subdivisions.add(area[2:])
            elif area.startswith("ct/") and len(area) > 3:
                used_cities.add(area[3:])

    # Categories (liste courte, on les affiche toutes)
    for c in _get_json(f"{PLAYLISTS_API_BASE}/categories.json", timeout):
        slug = (c.get("id") or "").strip()
        name = (c.get("name") or slug).strip()
        if not slug or not name:
            continue
        url = f"{IPTV_PLAYLIST_BASE}/categories/{slug.lower()}.m3u"
        add("Category", name, url)

    # Languages (filtreees par feeds.json)
    lang_name: dict[str, str] = {}
    if used_languages:
        # languages.json est gros: on ne garde que les codes utilises
        for l in _get_json(f"{PLAYLISTS_API_BASE}/languages.json", timeout):
            code = (l.get("code") or "").strip()
            if code in used_languages:
                lang_name[code] = (l.get("name") or code).strip() or code
                if len(lang_name) >= len(used_languages):
                    break

        for code in sorted(used_languages):
            name = lang_name.get(code, code)
            label = f"{name} ({code})" if name.lower() != code.lower() else code
            url = f"{IPTV_PLAYLIST_BASE}/languages/{code.lower()}.m3u"
            add("Language", label, url)

    # Countries (filtreees par feeds.json)
    country_name: dict[str, str] = {}
    countries = _get_json(f"{PLAYLISTS_API_BASE}/countries.json", timeout)
    for c in countries:
        code = (c.get("code") or "").strip()
        if code:
            country_name[code] = (c.get("name") or code).strip() or code

    for code in sorted(used_countries or country_name.keys()):
        name = country_name.get(code, code)
        label = f"{name} ({code})" if name else code
        url = f"{IPTV_PLAYLIST_BASE}/countries/{code.lower()}.m3u"
        add("Country", label, url)

    # Subdivisions (filtreees par feeds.json)
    if used_subdivisions:
        try:
            for s in _get_json(f"{PLAYLISTS_API_BASE}/subdivisions.json", timeout):
                code = (s.get("code") or "").strip()
                if code not in used_subdivisions:
                    continue
                name = (s.get("name") or code).strip() or code
                country = (s.get("country") or "").strip()
                label = f"{name} [{country}]" if country else name
                url = f"{IPTV_PLAYLIST_BASE}/subdivisions/{code.lower()}.m3u"
                add("Subdivision/City", label, url)
        except Exception:
            pass

    # Cities (filtreees par feeds.json, fichier lourd -> optionnel)
    if used_cities:
        try:
            for c in _get_json(f"{PLAYLISTS_API_BASE}/cities.json", timeout):
                code = (c.get("code") or "").strip()
                if code not in used_cities:
                    continue
                name = (c.get("name") or code).strip() or code
                country = (c.get("country") or "").strip()
                label = f"{name} [{country}]" if country else name
                url = f"{IPTV_PLAYLIST_BASE}/cities/{code.lower()}.m3u"
                add("Subdivision/City", label, url)
        except Exception:
            pass

    # Filtrage: garder uniquement les buckets non vides
    return {k: v for k, v in buckets.items() if v}


def _bucket_from_md(timeout: int) -> dict:
    """Fallback: parse PLAYLISTS.md si l'API est KO."""
    text = requests.get(PLAYLISTS_MD_RAW, timeout=timeout).text
    buckets = {"Category": [], "Language": [], "Country": [], "Subdivision/City": []}

    section = None
    in_code_fence = False

    for line in text.splitlines():
        l = line.strip()

        if "### Grouped by category" in l:
            section = "Category"
            continue
        if "### Grouped by language" in l:
            section = "Language"
            continue
        if "#### Countries" in l:
            section = "Country"
            continue
        if "### Grouped by broadcast area" in l:
            section = None
            continue

        if l.startswith("```"):
            in_code_fence = not in_code_fence
            continue

        if not section:
            continue

        m = CODE_URL_RE.search(line)
        if m:
            url = m.group(1).strip()

            name = ""
            row_m = TR_ROW_RE.search(line)
            if row_m:
                tds = TD_RE.findall(row_m.group(1))
                if tds:
                    name = strip_tags(tds[0])

            if not name:
                before = line.split("<code", 1)[0]
                name = strip_tags(before).strip(" -|")

            if not name:
                continue

            if section == "Country" and ("/subdivisions/" in url or "/cities/" in url):
                buckets["Subdivision/City"].append((name, url))
            else:
                buckets[section].append((name, url))
            continue

        m = BT_URL_RE.search(line)
        if m:
            url = m.group(1).strip()
            before = line.split("`", 1)[0]
            name = before.strip().lstrip("-").strip()
            name = re.sub(r"\s*\d+\s*$", "", name).strip()
            if not name:
                continue

            if section == "Country" and ("/subdivisions/" in url or "/cities/" in url):
                buckets["Subdivision/City"].append((name, url))
            else:
                buckets[section].append((name, url))
            continue

        if in_code_fence:
            m = PLAIN_URL_RE.match(line)
            if m:
                url = m.group(1).strip()
                if section == "Category":
                    name = "Index (grouped by category)"
                elif section == "Language":
                    name = "Index (grouped by language)"
                elif section == "Country":
                    name = "Index (countries)"
                else:
                    name = "Index"
                buckets[section].append((name, url))
            continue

    for k in buckets:
        seen = set()
        out = []
        for name, url in buckets[k]:
            key = (name, url)
            if key not in seen:
                seen.add(key)
                out.append((name, url))
        buckets[k] = out

    return buckets


def fetch_playlists_index(timeout=15) -> dict:
    """
    Charge d'abord depuis iptv-org/api (JSON cat/lang/pays/subdivisions), puis fallback sur PLAYLISTS.md.
    """
    api_err = None
    try:
        buckets = _bucket_from_api(timeout)
        if buckets:
            buckets["__source__"] = "api"
            return buckets
    except Exception as e:
        api_err = e

    try:
        buckets = _bucket_from_md(timeout)
        buckets["__source__"] = "md"
        return buckets
    except Exception as md_err:
        if api_err:
            raise RuntimeError(f"API iptv-org/api KO ({api_err}); fallback PLAYLISTS.md KO ({md_err})")
        raise


# =========================
# Feeds dialog (advanced listing + multi-criteria filtreers)
# =========================

class FeedsDialog(QtWidgets.QDialog):
    feeds_loaded = QtCore.Signal(list, dict)  # feeds, meta (names maps)
    feeds_error = QtCore.Signal(str)

    def __init__(self, parent: QtWidgets.QWidget, *, log):
        super().__init__(parent)
        self._log = log or (lambda _s: None)

        self.setWindowTitle("Feeds (API iptv-org)")
        self.resize(1100, 720)

        self._feeds_all: list[dict] = []
        self._visible_rows: list[int] = []
        self._max_rows = 5000

        layout = QtWidgets.QVBoxLayout(self)

        # Actions
        top = QtWidgets.QHBoxLayout()
        layout.addLayout(top)

        self.btn_refresh = QtWidgets.QPushButton("Recharger (API)")
        self.btn_search = QtWidgets.QPushButton("Rechercher")
        self.btn_import = QtWidgets.QPushButton("Importer la selection")
        self.btn_import.setEnabled(False)

        top.addWidget(self.btn_refresh)
        top.addWidget(self.btn_search)
        top.addWidget(self.btn_import)
        top.addStretch(1)
        self.lbl_cache = QtWidgets.QLabel("")
        top.addWidget(self.lbl_cache)

        # filtreers
        filt = QtWidgets.QGridLayout()
        layout.addLayout(filt)

        self.txt_q = QtWidgets.QLineEdit()
        self.txt_q.setPlaceholderText("Recherche (nom, channel, timezone, etc.)")

        self.cmb_country = QtWidgets.QComboBox()
        self.cmb_country.addItem("Tous pays", "")
        self.cmb_lang = QtWidgets.QComboBox()
        self.cmb_lang.addItem("Toutes langues", "")
        self.cmb_tz = QtWidgets.QComboBox()
        self.cmb_tz.addItem("Tous timezones", "")
        self.cmb_format = QtWidgets.QComboBox()
        self.cmb_format.addItem("Tous formats", "")
        self.chk_main = QtWidgets.QCheckBox("Main uniquement")

        filt.addWidget(QtWidgets.QLabel("Texte"), 0, 0)
        filt.addWidget(self.txt_q, 0, 1, 1, 5)
        filt.addWidget(QtWidgets.QLabel("Pays"), 1, 0)
        filt.addWidget(self.cmb_country, 1, 1)
        filt.addWidget(QtWidgets.QLabel("Langue"), 1, 2)
        filt.addWidget(self.cmb_lang, 1, 3)
        filt.addWidget(QtWidgets.QLabel("Timezone"), 1, 4)
        filt.addWidget(self.cmb_tz, 1, 5)
        filt.addWidget(QtWidgets.QLabel("Format"), 2, 0)
        filt.addWidget(self.cmb_format, 2, 1)
        filt.addWidget(self.chk_main, 2, 2, 1, 2)

        # Info
        self.lbl_info = QtWidgets.QLabel("")
        layout.addWidget(self.lbl_info)

        # Table
        self.tbl = QtWidgets.QTableWidget(0, 8)
        self.tbl.setHorizontalHeaderLabels([
            "Nom",
            "Channel",
            "Feed",
            "Main",
            "Pays",
            "Langues",
            "Timezones",
            "Format",
        ])
        self.tbl.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tbl.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setSortingEnabled(True)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.tbl, 1)

        # Signals
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_import.clicked.connect(self._import_selected)
        self.tbl.itemSelectionChanged.connect(self._sel_changed)

        self.txt_q.textChanged.connect(self.apply_filtreers)
        self.cmb_country.currentIndexChanged.connect(self.apply_filtreers)
        self.cmb_lang.currentIndexChanged.connect(self.apply_filtreers)
        self.cmb_tz.currentIndexChanged.connect(self.apply_filtreers)
        self.cmb_format.currentIndexChanged.connect(self.apply_filtreers)
        self.chk_main.stateChanged.connect(self.apply_filtreers)

        self.feeds_loaded.connect(self._on_loaded)
        self.feeds_error.connect(self._on_error)

        self.refresh()

    def _sel_changed(self):
        self.btn_import.setEnabled(len(self.tbl.selectionModel().selectedRows()) > 0)

    def _set_info(self, total: int, shown: int):
        if total <= shown:
            self.lbl_info.setText(f"Resultats: {shown}")
        else:
            self.lbl_info.setText(f"Resultats: {shown} / {total} (limite {self._max_rows} - ajuste les filtrees)")
        self._update_cache_label()

    def refresh(self):
        self.btn_refresh.setEnabled(False)
        self.btn_import.setEnabled(False)
        self.tbl.setRowCount(0)
        self._feeds_all = []
        self._visible_rows = []
        self.lbl_info.setText("Chargement feeds.json...")

        def run():
            try:
                feeds = fetch_feeds(timeout=30)

                # Light metadata for drop-down labels
                countries = _get_json(f"{PLAYLISTS_API_BASE}/countries.json", 30)
                languages = _get_json(f"{PLAYLISTS_API_BASE}/languages.json", 30)

                country_name = {c.get("code"): c.get("name") for c in (countries or []) if c.get("code")}
                lang_name = {l.get("code"): l.get("name") for l in (languages or []) if l.get("code")}

                self.feeds_loaded.emit(feeds, {"country_name": country_name, "lang_name": lang_name})
            except Exception as e:
                self.feeds_error.emit(str(e))

        threading.Thread(target=run, daemon=True).start()


class StreamsDialog(QtWidgets.QWidget):
    streams_loaded = QtCore.Signal(list, dict)  # streams, meta
    streams_error = QtCore.Signal(str)

    def __init__(self, parent: QtWidgets.QWidget, *, log, import_mode: str = "replace"):
        super().__init__(parent)
        self._log = log or (lambda _s: None)
        self._import_mode = (import_mode or "replace").strip().lower()
        # Garde une référence explicite sur la MainWindow pour l'import/merge,
        # car l'ajout dans un QTabWidget peut changer le parent Qt.
        self._main_window = parent

        self._streams_all: list[dict] = []
        self._visible_rows: list[int] = []
        self._max_rows = 5000
        self._meta: dict = {}
        self._cache_path = Path("data/streams_cache.json")
        self._last_fetch_ts: float | None = None

        layout = QtWidgets.QVBoxLayout(self)

        # Actions
        top = QtWidgets.QHBoxLayout()
        layout.addLayout(top)

        self.btn_refresh = QtWidgets.QPushButton("Recharger (API)")
        self.btn_search = QtWidgets.QPushButton("Rechercher")
        self.btn_import = QtWidgets.QPushButton("Importer la selection")
        self.btn_import.setEnabled(False)

        top.addWidget(self.btn_refresh)
        top.addWidget(self.btn_search)
        top.addWidget(self.btn_import)
        top.addStretch(1)
        self.lbl_cache = QtWidgets.QLabel("Cache: --")
        top.addWidget(self.lbl_cache)

        # filtreers
        filt = QtWidgets.QGridLayout()
        layout.addLayout(filt)

        self.txt_q = QtWidgets.QLineEdit()
        self.txt_q.setPlaceholderText("Recherche (title, channel, url, host, quality)...")

        self.cmb_country = QtWidgets.QComboBox()
        self.cmb_country.addItem("Tous pays", "")
        self.cmb_lang = QtWidgets.QComboBox()
        self.cmb_lang.addItem("Toutes langues", "")
        self.cmb_quality = QtWidgets.QComboBox()
        self.cmb_quality.addItem("Toutes qualites", "")

        self.chk_https = QtWidgets.QCheckBox("HTTPS uniquement")
        self.chk_ref = QtWidgets.QCheckBox("Avec referrer")
        self.chk_ua = QtWidgets.QCheckBox("Avec user-agent")

        filt.addWidget(QtWidgets.QLabel("Texte"), 0, 0)
        filt.addWidget(self.txt_q, 0, 1, 1, 5)
        filt.addWidget(QtWidgets.QLabel("Pays"), 1, 0)
        filt.addWidget(self.cmb_country, 1, 1)
        filt.addWidget(QtWidgets.QLabel("Langue"), 1, 2)
        filt.addWidget(self.cmb_lang, 1, 3)
        filt.addWidget(QtWidgets.QLabel("Qualite"), 1, 4)
        filt.addWidget(self.cmb_quality, 1, 5)
        filt.addWidget(self.chk_https, 2, 1)
        filt.addWidget(self.chk_ref, 2, 3)
        filt.addWidget(self.chk_ua, 2, 5)

        self.lbl_info = QtWidgets.QLabel("")
        layout.addWidget(self.lbl_info)

        # Table
        self.tbl = QtWidgets.QTableWidget(0, 9)
        self.tbl.setHorizontalHeaderLabels([
            "Titre",
            "Channel",
            "Feed",
            "Qualite",
            "Pays",
            "Langues",
            "Host",
            "URL",
            "Options",
        ])
        self.tbl.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tbl.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setSortingEnabled(True)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.tbl, 1)

        # Signals
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_search.clicked.connect(self.apply_filtreers)
        self.btn_import.clicked.connect(self._import_selected)
        self.tbl.itemSelectionChanged.connect(self._sel_changed)
        self.txt_q.returnPressed.connect(self.apply_filtreers)

        self.streams_loaded.connect(self._on_loaded)
        self.streams_error.connect(self._on_error)

        # Charger un cache local si disponible pour rechercher sans appel réseau
        self._load_cache()

    def _sel_changed(self):
        self.btn_import.setEnabled(len(self.tbl.selectionModel().selectedRows()) > 0)

    def set_import_mode(self, mode: str):
        mode = (mode or "replace").strip().lower()
        self._import_mode = "merge" if mode == "merge" else "replace"
        if self._import_mode == "merge":
            self.btn_import.setText("Fusionner la selection")
        else:
            self.btn_import.setText("Importer la selection")

    def ensure_loaded(self):
        if not self._streams_all and self.btn_refresh.isEnabled():
            self.refresh()

    def _set_info(self, total: int, shown: int):
        if total <= shown:
            self.lbl_info.setText(f"Resultats: {shown}")
        else:
            self.lbl_info.setText(f"Resultats: {shown} / {total} (limite {self._max_rows} - ajuste les filtrees)")

    def refresh(self):
        self.btn_refresh.setEnabled(False)
        self.btn_import.setEnabled(False)
        self.tbl.setRowCount(0)
        self._streams_all = []
        self._visible_rows = []
        self.lbl_info.setText("Chargement streams.json...")

        def run():
            try:
                streams = fetch_streams(timeout=45)
                feeds = fetch_feeds(timeout=45)

                countries = _get_json(f"{PLAYLISTS_API_BASE}/countries.json", 30)
                languages = _get_json(f"{PLAYLISTS_API_BASE}/languages.json", 30)

                country_name = {c.get("code"): c.get("name") for c in (countries or []) if c.get("code")}
                lang_name = {l.get("code"): l.get("name") for l in (languages or []) if l.get("code")}

                feed_meta: dict[tuple[str, str], dict] = {}
                used_countries: set[str] = set()
                used_langs: set[str] = set()
                for f in feeds or []:
                    ch = (f.get("channel") or "").strip()
                    fid = (f.get("id") or "").strip()
                    if not ch or not fid:
                        continue
                    countries_codes = []
                    for area in (f.get("broadcast_area") or []):
                        if isinstance(area, str) and area.startswith("c/") and len(area) > 2:
                            countries_codes.append(area[2:])
                    langs = [x for x in (f.get("languages") or []) if isinstance(x, str) and x.strip()]
                    for ccode in countries_codes:
                        used_countries.add(ccode)
                    for lcode in langs:
                        used_langs.add(lcode)
                    feed_meta[(ch, fid)] = {
                        "countries": sorted(set(countries_codes)),
                        "languages": sorted(set(langs)),
                        "timezones": [x for x in (f.get("timezones") or []) if isinstance(x, str) and x.strip()],
                        "format": (f.get("format") or "").strip(),
                        "name": (f.get("name") or "").strip(),
                        "is_main": bool(f.get("is_main")),
                    }

                meta = {
                    "country_name": country_name,
                    "lang_name": lang_name,
                    "feed_meta": feed_meta,
                    "used_countries": used_countries,
                        "used_langs": used_langs,
                }

                self.streams_loaded.emit(streams, meta)
            except Exception as e:
                self.streams_error.emit(str(e))

        threading.Thread(target=run, daemon=True).start()

    @QtCore.Slot(str)
    def _feeds_on_error(self, err: str):
        self.btn_refresh.setEnabled(True)
        self.lbl_info.setText(f"Erreur API: {err}")
        self._log(f"Streams: erreur API: {err}")

    @QtCore.Slot(list, dict)
    def _on_loaded(self, streams: list[dict], meta: dict):
        self.btn_refresh.setEnabled(True)
        self._streams_all = streams or []
        self._meta = meta or {}
        self._last_fetch_ts = time.time()

        self._populate_filters_from_meta()
        self.apply_filtreers()
        self._save_cache(self._streams_all, self._meta, self._last_fetch_ts)
        self._log(f"Streams: chargement OK ({len(self._streams_all)}).")

    def _populate_filters_from_meta(self):
        used_countries = self._meta.get("used_countries") or set()
        used_langs = self._meta.get("used_langs") or set()
        country_name = self._meta.get("country_name") or {}
        lang_name = self._meta.get("lang_name") or {}

        # Populate combos based on metadata from feeds + streams qualities
        def refill(cmb: QtWidgets.QComboBox, label_all: str, values: list[tuple[str, str]]):
            cmb.blockSignals(True)
            cmb.clear()
            cmb.addItem(label_all, "")
            for label, data in values:
                cmb.addItem(label, data)
            cmb.blockSignals(False)

        country_vals = []
        for code in sorted(used_countries):
            name = country_name.get(code) or code
            country_vals.append((f"{name} ({code})", code))
        refill(self.cmb_country, "Tous pays", country_vals)

        lang_vals = []
        for code in sorted(used_langs):
            name = lang_name.get(code) or code
            lang_vals.append((f"{name} ({code})", code))
        refill(self.cmb_lang, "Toutes langues", lang_vals)

        qualities: set[str] = set()
        for s in self._streams_all:
            q = (s.get("quality") or "").strip()
            if q:
                qualities.add(q)
        qual_vals = [(q, q) for q in sorted(qualities)]
        refill(self.cmb_quality, "Toutes qualites", qual_vals)
        self._update_cache_label()

    def _stream_countries_langs(self, s: dict) -> tuple[list[str], list[str]]:
        feed_meta = self._meta.get("feed_meta") or {}
        ch = (s.get("channel") or "").strip()
        fid = (s.get("feed") or "").strip()
        meta = feed_meta.get((ch, fid)) if ch and fid else None
        if not meta:
            return [], []
        return list(meta.get("countries") or []), list(meta.get("languages") or [])

    def _update_cache_label(self):
        if not self._last_fetch_ts:
            self.lbl_cache.setText("Cache: --")
            return
        age = max(0, time.time() - float(self._last_fetch_ts))
        if age < 60:
            age_txt = f"{int(age)}s"
        elif age < 3600:
            age_txt = f"{int(age // 60)}m"
        elif age < 86400:
            age_txt = f"{int(age // 3600)}h"
        else:
            age_txt = f"{int(age // 86400)}j"
        self.lbl_cache.setText(f"Cache: {age_txt}")

    @staticmethod
    def _serialize_meta(meta: dict) -> dict:
        feed_meta_src = meta.get("feed_meta") or {}
        feed_meta_list = []
        for (ch, fid), m in feed_meta_src.items():
            feed_meta_list.append(
                {
                    "channel": ch,
                    "feed": fid,
                    "countries": list(m.get("countries") or []),
                    "languages": list(m.get("languages") or []),
                    "timezones": list(m.get("timezones") or []),
                    "format": m.get("format") or "",
                    "name": m.get("name") or "",
                    "is_main": bool(m.get("is_main")),
                }
            )
        return {
            "country_name": meta.get("country_name") or {},
            "lang_name": meta.get("lang_name") or {},
            "feed_meta": feed_meta_list,
            "used_countries": list(meta.get("used_countries") or []),
            "used_langs": list(meta.get("used_langs") or []),
        }

    @staticmethod
    def _deserialize_meta(data: dict) -> dict:
        feed_meta_list = data.get("feed_meta") or []
        feed_meta = {}
        for item in feed_meta_list:
            ch = (item.get("channel") or "").strip()
            fid = (item.get("feed") or "").strip()
            if not ch or not fid:
                continue
            feed_meta[(ch, fid)] = {
                "countries": list(item.get("countries") or []),
                "languages": list(item.get("languages") or []),
                "timezones": list(item.get("timezones") or []),
                "format": item.get("format") or "",
                "name": item.get("name") or "",
                "is_main": bool(item.get("is_main")),
            }
        return {
            "country_name": data.get("country_name") or {},
            "lang_name": data.get("lang_name") or {},
            "feed_meta": feed_meta,
            "used_countries": set(data.get("used_countries") or []),
            "used_langs": set(data.get("used_langs") or []),
        }

    def _save_cache(self, streams: list[dict], meta: dict, ts: float):
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "ts": ts,
                "streams": streams,
                "meta": self._serialize_meta(meta),
            }
            self._cache_path.write_text(json.dumps(payload), encoding="utf-8")
        except Exception as e:
            self._log(f"Streams: cache save KO: {e}")

    def _load_cache(self):
        try:
            raw = self._cache_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except FileNotFoundError:
            self._update_cache_label()
            return
        except Exception as e:
            self._log(f"Streams: cache lecture KO: {e}")
            self._update_cache_label()
            return

        try:
            self._streams_all = data.get("streams") or []
            self._meta = self._deserialize_meta(data.get("meta") or {})
            ts_val = data.get("ts")
            self._last_fetch_ts = float(ts_val) if ts_val is not None else None
            self._populate_filters_from_meta()
            self.apply_filtreers()
            self._log(f"Streams: cache charge ({len(self._streams_all)}).")
        except Exception as e:
            self._log(f"Streams: cache invalide: {e}")
        finally:
            self._update_cache_label()

    def apply_filtreers(self):
        q = (self.txt_q.text() or "").strip().lower()
        wanted_country = self.cmb_country.currentData() or ""
        wanted_lang = self.cmb_lang.currentData() or ""
        wanted_quality = self.cmb_quality.currentData() or ""
        https_only = self.chk_https.isChecked()
        need_ref = self.chk_ref.isChecked()
        need_ua = self.chk_ua.isChecked()

        self._visible_rows.clear()
        for i, s in enumerate(self._streams_all):
            url = (s.get("url") or "").strip()
            if not url:
                continue
            parsed = urlparse(url)
            if https_only and parsed.scheme.lower() != "https":
                continue

            if need_ref and not (s.get("referrer") or "").strip():
                continue
            if need_ua and not (s.get("user_agent") or "").strip():
                continue

            if wanted_quality and (s.get("quality") or "").strip() != wanted_quality:
                continue

            countries, langs = self._stream_countries_langs(s)
            if wanted_country and wanted_country not in countries:
                continue
            if wanted_lang and wanted_lang not in langs:
                continue

            if q:
                host = (parsed.hostname or "")
                hay = " ".join([
                    str(s.get("title") or ""),
                    str(s.get("channel") or ""),
                    str(s.get("feed") or ""),
                    str(s.get("quality") or ""),
                    host,
                    url,
                ]).lower()
                if q not in hay:
                    continue

            self._visible_rows.append(i)
            if len(self._visible_rows) >= self._max_rows:
                break

        self._render_table()
        self._set_info(total=len(self._streams_all), shown=len(self._visible_rows))

    def _render_table(self):
        self.tbl.setSortingEnabled(False)
        feed_meta = self._meta.get("feed_meta") or {}
        country_name = self._meta.get("country_name") or {}

        self.tbl.setRowCount(len(self._visible_rows))
        for r, src_i in enumerate(self._visible_rows):
            s = self._streams_all[src_i]
            title = (s.get("title") or "").strip()
            ch = (s.get("channel") or "").strip()
            fid = (s.get("feed") or "").strip()
            quality = (s.get("quality") or "").strip()
            url = (s.get("url") or "").strip()
            parsed = urlparse(url)
            host = (parsed.hostname or "").strip()

            countries, langs = self._stream_countries_langs(s)
            countries_txt = ", ".join([f"{country_name.get(c) or c} ({c})" for c in countries]) if countries else ""
            langs_txt = ", ".join(langs) if langs else ""

            opts = []
            if (s.get("referrer") or "").strip():
                opts.append("referrer")
            if (s.get("user_agent") or "").strip():
                opts.append("ua")

            title_item = QtWidgets.QTableWidgetItem(title or "(sans titre)")
            title_item.setData(QtCore.Qt.ItemDataRole.UserRole, int(src_i))

            # Tooltip: show feed name if known
            try:
                meta = feed_meta.get((ch, fid)) if ch and fid else None
                if meta:
                    title_item.setToolTip(f"feed_name={meta.get('name')}\nformat={meta.get('format')}\ntimezones={','.join(meta.get('timezones') or [])}")
            except Exception:
                pass

            self.tbl.setItem(r, 0, title_item)
            self.tbl.setItem(r, 1, QtWidgets.QTableWidgetItem(ch))
            self.tbl.setItem(r, 2, QtWidgets.QTableWidgetItem(fid))
            self.tbl.setItem(r, 3, QtWidgets.QTableWidgetItem(quality))
            self.tbl.setItem(r, 4, QtWidgets.QTableWidgetItem(countries_txt))
            self.tbl.setItem(r, 5, QtWidgets.QTableWidgetItem(langs_txt))
            self.tbl.setItem(r, 6, QtWidgets.QTableWidgetItem(host))
            self.tbl.setItem(r, 7, QtWidgets.QTableWidgetItem(url))
            self.tbl.setItem(r, 8, QtWidgets.QTableWidgetItem(", ".join(opts)))

        self.tbl.resizeColumnsToContents()
        self.tbl.setSortingEnabled(True)

    def _selected_streams(self) -> list[dict]:
        sel = self.tbl.selectionModel().selectedRows()
        if not sel:
            return []
        out = []
        for idx in sel:
            row = idx.row()
            if row < 0 or row >= self.tbl.rowCount():
                continue
            item = self.tbl.item(row, 0)
            if not item:
                continue
            src_i = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if isinstance(src_i, int) and 0 <= src_i < len(self._streams_all):
                out.append(self._streams_all[int(src_i)])
        return out

    def _import_selected(self):
        selected = self._selected_streams()
        if not selected:
            return

        out = ["#EXTM3U"]
        for s in selected:
            url = (s.get("url") or "").strip()
            if not url:
                continue

            ch = (s.get("channel") or "").strip()
            title = (s.get("title") or "").strip() or ch or "Stream"
            quality = (s.get("quality") or "").strip()
            label = f"{title} [{quality}]" if quality else title

            extinf = f'#EXTINF:-1 tvg-id="{ch}"' if ch else "#EXTINF:-1"
            out.append(extinf + f",{label}")

            ref = (s.get("referrer") or "").strip()
            ua = (s.get("user_agent") or "").strip()
            if ref:
                out.append(f"#EXTVLCOPT:http-referrer={ref}")
            if ua:
                out.append(f"#EXTVLCOPT:http-user-agent={ua}")

            out.append(url)

        m3u = "\n".join(out) + "\n"
        label = f"API streams ({len(selected)} selection)"

        main = self._main_window or self.parent() or self.window()
        if self._import_mode == "merge":
            merge_fn = getattr(main, "_merge_channels", None)
            if merge_fn is not None:
                try:
                    merge_fn(parse_m3u(m3u), label)
                    self._log(f"Streams: fusion OK -> {len(selected)} streams.")
                except Exception as e:
                    self._log(f"Streams: fusion KO: {e}")
                return
            # Fallback to import if merge unavailable

        import_emit = getattr(main, "import_merged", None)
        if import_emit is not None:
            import_emit.emit(m3u, label)
        self._log(f"Streams: import OK -> {len(selected)} streams.")

    @QtCore.Slot(str)
    def _on_error(self, err: str):
        self.btn_refresh.setEnabled(True)
        self.lbl_info.setText(f"Erreur API: {err}")
        self._log(f"Feeds: erreur API: {err}")

    @QtCore.Slot(list, dict)
    def _feeds_on_loaded(self, feeds: list[dict], meta: dict):
        self.btn_refresh.setEnabled(True)
        self._feeds_all = feeds or []

        country_name = meta.get("country_name") or {}
        lang_name = meta.get("lang_name") or {}

        # Populate filtreer combos from feeds content (only used values)
        countries_used: set[str] = set()
        langs_used: set[str] = set()
        tz_used: set[str] = set()
        fmt_used: set[str] = set()

        for f in self._feeds_all:
            for area in (f.get("broadcast_area") or []):
                if isinstance(area, str) and area.startswith("c/") and len(area) > 2:
                    countries_used.add(area[2:])
            for l in (f.get("languages") or []):
                if isinstance(l, str) and l.strip():
                    langs_used.add(l.strip())
            for tz in (f.get("timezones") or []):
                if isinstance(tz, str) and tz.strip():
                    tz_used.add(tz.strip())
            fmt = (f.get("format") or "").strip()
            if fmt:
                fmt_used.add(fmt)

        def refill(cmb: QtWidgets.QComboBox, label_all: str, values: list[tuple[str, str]]):
            cmb.blockSignals(True)
            cmb.clear()
            cmb.addItem(label_all, "")
            for label, data in values:
                cmb.addItem(label, data)
            cmb.blockSignals(False)

        country_vals = []
        for code in sorted(countries_used):
            name = country_name.get(code) or code
            country_vals.append((f"{name} ({code})", code))
        refill(self.cmb_country, "Tous pays", country_vals)

        lang_vals = []
        for code in sorted(langs_used):
            name = lang_name.get(code) or code
            lang_vals.append((f"{name} ({code})", code))
        refill(self.cmb_lang, "Toutes langues", lang_vals)

        tz_vals = [(tz, tz) for tz in sorted(tz_used)]
        refill(self.cmb_tz, "Tous timezones", tz_vals)

        fmt_vals = [(f, f) for f in sorted(fmt_used)]
        refill(self.cmb_format, "Tous formats", fmt_vals)

        self._feeds_apply_filtreers()
        self._log(f"Feeds: chargement OK ({len(self._feeds_all)}).")

    def _feeds_country_pass(self, f: dict, wanted_code: str) -> bool:
        if not wanted_code:
            return True
        for area in (f.get("broadcast_area") or []):
            if isinstance(area, str) and area.strip() == f"c/{wanted_code}":
                return True
        return False

    def _feeds_lang_pass(self, f: dict, wanted_code: str) -> bool:
        if not wanted_code:
            return True
        langs = f.get("languages") or []
        return wanted_code in langs

    def _feeds_tz_pass(self, f: dict, wanted_tz: str) -> bool:
        if not wanted_tz:
            return True
        tzs = f.get("timezones") or []
        return wanted_tz in tzs

    def _feeds_fmt_pass(self, f: dict, wanted_fmt: str) -> bool:
        if not wanted_fmt:
            return True
        return (f.get("format") or "").strip() == wanted_fmt

    def _feeds_apply_filtreers(self):
        q = (self.txt_q.text() or "").strip().lower()
        country = self.cmb_country.currentData() or ""
        lang = self.cmb_lang.currentData() or ""
        tz = self.cmb_tz.currentData() or ""
        fmt = self.cmb_format.currentData() or ""
        main_only = self.chk_main.isChecked()

        self._visible_rows.clear()
        for i, f in enumerate(self._feeds_all):
            if main_only and (not bool(f.get("is_main"))):
                continue
            if not self._feeds_country_pass(f, country):
                continue
            if not self._feeds_lang_pass(f, lang):
                continue
            if not self._feeds_tz_pass(f, tz):
                continue
            if not self._feeds_fmt_pass(f, fmt):
                continue

            if q:
                hay = " ".join([
                    str(f.get("name") or ""),
                    str(f.get("channel") or ""),
                    str(f.get("id") or ""),
                    " ".join(f.get("broadcast_area") or []),
                    " ".join(f.get("timezones") or []),
                    " ".join(f.get("languages") or []),
                    str(f.get("format") or ""),
                ]).lower()
                if q not in hay:
                    continue

            self._visible_rows.append(i)
            if len(self._visible_rows) >= self._max_rows:
                break

        self._feeds_render_table()
        self._set_info(total=len(self._feeds_all), shown=len(self._visible_rows))

    def _feeds_render_table(self):
        self.tbl.setSortingEnabled(False)
        self.tbl.setRowCount(len(self._visible_rows))
        for r, src_i in enumerate(self._visible_rows):
            f = self._feeds_all[src_i]
            name = (f.get("name") or "").strip()
            channel = (f.get("channel") or "").strip()
            feed_id = (f.get("id") or "").strip()
            is_main = "Yes" if f.get("is_main") else ""

            # UX: dans beaucoup de cas `name` correspond à une zone (souvent un pays/ville).
            # On préfixe donc par le channel pour que la colonne "Nom" soit plus parlante.
            display_name = name
            if channel and name and channel.lower() not in name.lower():
                display_name = f"{channel} — {name}"
            elif channel and not name:
                display_name = channel
            elif (not channel) and not name:
                display_name = "(sans nom)"

            countries = []
            for area in (f.get("broadcast_area") or []):
                if isinstance(area, str) and area.startswith("c/") and len(area) > 2:
                    countries.append(area[2:])
            countries_txt = ", ".join(sorted(set(countries)))

            langs_txt = ", ".join([str(x) for x in (f.get("languages") or []) if x])
            tz_txt = ", ".join([str(x) for x in (f.get("timezones") or []) if x])
            fmt = (f.get("format") or "").strip()

            name_item = QtWidgets.QTableWidgetItem(display_name)
            name_item.setData(QtCore.Qt.ItemDataRole.UserRole, int(src_i))
            # Détails complets en tooltip
            try:
                ba_txt = ", ".join([str(x) for x in (f.get("broadcast_area") or []) if x])
                tt = f"channel={channel}\nfeed={feed_id}\nname={name}\narea={ba_txt}"
                name_item.setToolTip(tt)
            except Exception:
                pass
            self.tbl.setItem(r, 0, name_item)
            self.tbl.setItem(r, 1, QtWidgets.QTableWidgetItem(channel))
            self.tbl.setItem(r, 2, QtWidgets.QTableWidgetItem(feed_id))
            self.tbl.setItem(r, 3, QtWidgets.QTableWidgetItem(is_main))
            self.tbl.setItem(r, 4, QtWidgets.QTableWidgetItem(countries_txt))
            self.tbl.setItem(r, 5, QtWidgets.QTableWidgetItem(langs_txt))
            self.tbl.setItem(r, 6, QtWidgets.QTableWidgetItem(tz_txt))
            self.tbl.setItem(r, 7, QtWidgets.QTableWidgetItem(fmt))

        self.tbl.resizeColumnsToContents()
        self.tbl.setSortingEnabled(True)

    def _feeds_selected_feeds(self) -> list[dict]:
        sel = self.tbl.selectionModel().selectedRows()
        if not sel:
            return []
        out = []
        for idx in sel:
            row = idx.row()
            if row < 0 or row >= self.tbl.rowCount():
                continue
            item = self.tbl.item(row, 0)
            if not item:
                continue
            src_i = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if isinstance(src_i, int) and 0 <= src_i < len(self._feeds_all):
                out.append(self._feeds_all[int(src_i)])
        return out

    def _feeds_import_selected(self):
        selected = self._feeds_selected_feeds()
        if not selected:
            return

        self.btn_import.setEnabled(False)
        self.btn_refresh.setEnabled(False)
        self.lbl_info.setText("Chargement streams.json + generation M3U...")

        # Delegate import to parent MainWindow via a callback if available.
        parent = self.parent()
        import_emit = getattr(parent, "import_merged", None)
        log = self._log

        def run():
            try:
                streams = fetch_streams(timeout=45)
                m3u = build_m3u_from_api_streams(selected, streams, max_streams_per_feed=6)
                label = f"API feeds ({len(selected)} selection)"

                if import_emit is not None:
                    import_emit.emit(m3u, label)
                log(f"Feeds: import OK -> {len(selected)} feeds.")
            except Exception as e:
                log(f"Feeds: erreur import: {e}")
            finally:
                QtCore.QTimer.singleShot(0, self, lambda: self.btn_refresh.setEnabled(True))
                QtCore.QTimer.singleShot(0, self, lambda: self.btn_import.setEnabled(True))
                QtCore.QTimer.singleShot(0, self, lambda: self._feeds_apply_filtreers())

        threading.Thread(target=run, daemon=True).start()


# =========================
# EPG Guide Dialog
# =========================

class EpgDialog(QtWidgets.QDialog):
    """Modal dialog to browse EPG entries for a single tvg-id."""

    def __init__(self, parent: QtWidgets.QWidget, db: Storage, tvg_id: str, channel_name: str):
        super().__init__(parent)
        self.db = db
        self.tvg_id = tvg_id
        self.channel_name = channel_name
        self._rows: list[dict] = []

        self.setWindowTitle(f"Guide EPG — {channel_name} ({tvg_id})")
        self.resize(950, 650)

        layout = QtWidgets.QVBoxLayout(self)

        top = QtWidgets.QHBoxLayout()
        layout.addLayout(top)


        self.hours = QtWidgets.QSpinBox()
        self.hours.setRange(1, 72)
        self.hours.setValue(24)

        self.btn_refresh = QtWidgets.QPushButton("Afficher")

        top.addWidget(QtWidgets.QLabel("Plage (heures)"))
        top.addWidget(self.hours)
        top.addStretch(1)
        top.addWidget(self.btn_refresh)

        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Début", "Fin", "Titre"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)

        self.desc = QtWidgets.QPlainTextEdit()
        self.desc.setReadOnly(True)
        self.desc.setMaximumBlockCount(3000)
        layout.addWidget(self.desc, 0)

        self.btn_refresh.clicked.connect(self.refresh)
        self.table.itemSelectionChanged.connect(self._on_select)

        self.refresh()

    def _fmt_ts(self, ts: int) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))

    def refresh(self):
        # ✅ plage: maintenant -> maintenant + N heures
        start_ts = int(time.time())
        stop_ts = start_ts + int(self.hours.value()) * 3600

        self._rows = self.db.list_epg_programs(self.tvg_id, start_ts, stop_ts, limit=2000)

        self.table.setRowCount(len(self._rows))
        for i, p in enumerate(self._rows):
            self.table.setItem(i, 0, QtWidgets.QTableWidgetItem(self._fmt_ts(p["start_ts"])))
            self.table.setItem(i, 1, QtWidgets.QTableWidgetItem(self._fmt_ts(p["stop_ts"])))
            self.table.setItem(i, 2, QtWidgets.QTableWidgetItem(p.get("title", "") or ""))

        self.table.resizeColumnsToContents()

        if not self._rows:
            self.desc.setPlainText("(Aucun programme dans cette plage.)")
        else:
            self.desc.setPlainText("Sélectionne une émission pour voir la description.")


    def _on_select(self):
        sel = self.table.selectionModel().selectedRows()
        if not sel:
            return
        r = sel[0].row()
        if r < 0 or r >= len(self._rows):
            return

        p = self._rows[r]
        st = self._fmt_ts(p["start_ts"])
        en = self._fmt_ts(p["stop_ts"])
        title = (p.get("title") or "").strip()
        desc = (p.get("desc") or "").strip()

        txt = f"{st} → {en}\n{title}\n\n{desc}" if desc else f"{st} → {en}\n{title}"
        self.desc.setPlainText(txt)


# =========================
# UI
# =========================

class MainWindow(QtWidgets.QMainWindow):
    """
    Main UI container: playlists browser (GitHub), playlist editor, VLC player, and Salon
    (local DB). Coordinates network downloads, EPG import, and background probes.
    """
    playlists_loaded = QtCore.Signal(dict)
    playlists_error = QtCore.Signal(str)
    import_merged = QtCore.Signal(str, str)

    epg_ok = QtCore.Signal()
    epg_fail = QtCore.Signal(str)
    epg_progress = QtCore.Signal(str)
    epg_progress_value = QtCore.Signal(int)  # -1 = indeterminate, 0-100 = percent

    log_sig = QtCore.Signal(int, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("IPTV Cleaner (PySide6)")
        self.resize(1200, 720)

        self.config_path = Path("data/config.json")
        self._current_style = "Fusion"
        self._current_theme = "light"
        self._current_epg_path = ""

        self.channels: list[Channel] = []
        self._probe_thread: QtCore.QThread | None = None
        self._probe_worker: ProbeWorker | None = None
        self._probe_total: int = 0
        self._probe_done: int = 0
        self._editing_playlist_id: int | None = None
        self._editing_playlist_name: str | None = None
        self._last_import_source: str = "-"  # rappel de provenance pour l'export Salon
        self._last_epg_xml: bytes | None = None
        self._last_epg_coverage: str = ""
        self._epg_cache_dir = Path("data/epg_cache")
        self._epg_cache_ttl_hours = 12

        # DB
        self.db = Storage("data/iptv.db")
        self.epg_loaded = False

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # Splitter vertical: onglets en haut, log global en bas
        self.vsplit = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        layout.addWidget(self.vsplit, 1)

        # Tabs
        self.tabs = QtWidgets.QTabWidget()
        self.vsplit.addWidget(self.tabs)

        # ---- Tab 1: Browser
        tab_browser = QtWidgets.QWidget()
        vb = QtWidgets.QVBoxLayout(tab_browser)

        hb = QtWidgets.QHBoxLayout()
        vb.addLayout(hb)

        self.btn_refresh_lists = QtWidgets.QPushButton("Lister playlists (API iptv-org)")
        self.btn_open_streams = QtWidgets.QPushButton("Streams (API)…")
        self.btn_load_selected_list = QtWidgets.QPushButton("Charger la sélection")
        self.btn_load_selected_list.setEnabled(False)

        self.list_search = QtWidgets.QLineEdit()
        self.list_search.setPlaceholderText("Rechercher (ex: french, canada, sports)…")

        hb.addWidget(self.btn_refresh_lists)
        hb.addWidget(self.btn_open_streams)
        hb.addWidget(self.btn_load_selected_list)
        hb.addWidget(self.list_search, 1)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Playlist", "URL"])
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setSortingEnabled(True)
        self.tree.setColumnWidth(0, 420)
        self.tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.tree.setRootIsDecorated(True)
        self.tree.setExpandsOnDoubleClick(True)

        vb.addWidget(self.tree, 1)
        self.tabs.addTab(tab_browser, "Playlists (API)")

        # ---- Tab 2: Channels (éditeur playlist)
        tab_channels = QtWidgets.QWidget()
        vc = QtWidgets.QVBoxLayout(tab_channels)

        # Actions (les boutons que tu voulais DANS l'éditeur)
        actions = QtWidgets.QHBoxLayout()
        vc.addLayout(actions)

        # Menus: Importer / Fusionner / Supprimer / Exporter
        self.btn_import = QtWidgets.QToolButton()
        self.btn_import.setText("Importer")
        self.btn_import.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self._import_menu = QtWidgets.QMenu(self.btn_import)
        self.act_import_file = self._import_menu.addAction("Importer M3U (fichier)")
        self.act_import_url = self._import_menu.addAction("Importer M3U (URL)")
        self.btn_import.setMenu(self._import_menu)

        self.btn_merge = QtWidgets.QToolButton()
        self.btn_merge.setText("Fusionner")
        self.btn_merge.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self._merge_menu = QtWidgets.QMenu(self.btn_merge)
        self.act_merge_file = self._merge_menu.addAction("Fusionner M3U depuis fichier")
        self.act_merge_url = self._merge_menu.addAction("Fusionner M3U depuis URL")
        self.act_merge_salon = self._merge_menu.addAction("Fusionner playlist du Salon")
        self.act_merge_txt_links = self._merge_menu.addAction("Fusionner liens (fichier .txt)")
        self.act_merge_streams_api = self._merge_menu.addAction("Fusionner Streams (API)")
        self.btn_merge.setMenu(self._merge_menu)

        self.btn_delete_menu = QtWidgets.QToolButton()
        self.btn_delete_menu.setText("Supprimer")
        self.btn_delete_menu.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self._delete_menu = QtWidgets.QMenu(self.btn_delete_menu)
        self.act_del_dead = self._delete_menu.addAction("Supprimer KO")
        self.act_del_sel = self._delete_menu.addAction("Supprimer selection")
        self.btn_delete_menu.setMenu(self._delete_menu)

        self.btn_export_menu = QtWidgets.QToolButton()
        self.btn_test = QtWidgets.QPushButton("Tester URLs")
        self.btn_stop = QtWidgets.QPushButton("Stop")
        self.btn_send_player = QtWidgets.QPushButton("Charger dans le lecteur")
        self.btn_stop.setEnabled(False)
        self.lbl_probe_status = QtWidgets.QLabel("")

        self.btn_export_menu.setText("Exporter")
        self.btn_export_menu.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self._export_menu = QtWidgets.QMenu(self.btn_export_menu)
        self.act_export_filtered = self._export_menu.addAction("Exporter playlist filtree")
        self.act_export_salon = self._export_menu.addAction("Exporter au Salon")
        self.btn_export_menu.setMenu(self._export_menu)
        actions.addWidget(self.btn_import)
        actions.addWidget(self.btn_merge)
        actions.addWidget(self.btn_test)
        actions.addWidget(self.btn_stop)
        actions.addWidget(self.btn_send_player)
        actions.addSpacing(12)
        actions.addWidget(self.btn_delete_menu)
        actions.addWidget(self.btn_export_menu)
        actions.addStretch(1)
        actions.addWidget(self.lbl_probe_status)


        # filtree
        filt = QtWidgets.QHBoxLayout()
        vc.addLayout(filt)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("filtree chaînes (nom, groupe, tvg-id, url)…")
        filt.addWidget(self.search)

        # Table chaînes
        self.table = QtWidgets.QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Nom", "Groupe", "tvg-id", "Risque", "Raisons", "Statut", "URL"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        vc.addWidget(self.table, 1)

        # ---- EPG UI
        self.epg_box = QtWidgets.QGroupBox("EPG (XMLTV)")
        epg_layout = QtWidgets.QVBoxLayout(self.epg_box)

        epg_top = QtWidgets.QHBoxLayout()
        epg_layout.addLayout(epg_top)

        self.epg_url = QtWidgets.QLineEdit("")
        self.epg_url.setPlaceholderText("URL EPG (ex: http://localhost:3000/guide.xml ou .xml.gz)")
        self.btn_epg_update = QtWidgets.QPushButton("Mettre à jour EPG")

        self.btn_epg_guide = QtWidgets.QPushButton("Guide…")
        self.btn_epg_guide.setEnabled(False)

        epg_top.addWidget(self.epg_url, 1)
        epg_top.addWidget(self.btn_epg_update)
        epg_top.addWidget(self.btn_epg_guide)

        epg_opts = QtWidgets.QHBoxLayout()
        epg_layout.addLayout(epg_opts)
        self.chk_epg_auto = QtWidgets.QCheckBox("Auto EPG pour playlists Salon (cache 12h)")
        self.chk_epg_auto.setChecked(True)
        self.btn_epg_export = QtWidgets.QPushButton("Exporter EPG (snapshot)")
        epg_opts.addWidget(self.chk_epg_auto)
        epg_opts.addStretch(1)
        epg_opts.addWidget(self.btn_epg_export)

        self.lbl_epg_status = QtWidgets.QLabel("")
        self.lbl_epg_status.setWordWrap(True)

        # Garder l'EPG compact pour laisser la place au tableau
        self.epg_box.setMaximumHeight(160)
        self.epg_box.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Maximum)

        epg_layout.addWidget(self.lbl_epg_status)
        vc.addWidget(self.epg_box)

        self.channels_tab = tab_channels
        self.tabs.addTab(tab_channels, "Chaînes")

        # ---- Tab 3: Streams API (widget intégré) - après Chaînes
        self.streams_widget = StreamsDialog(self, log=self.logln, import_mode="replace")
        self.streams_tab_index = self.tabs.addTab(self.streams_widget, "Streams (API)")

        # ---- Tab 3: VLC Player (création paresseuse)
        tab_player = QtWidgets.QWidget()
        self._player_layout = QtWidgets.QVBoxLayout(tab_player)
        self.player_widget: VlcPlayerPanel | None = None
        self._player_placeholder = QtWidgets.QLabel("Lecteur VLC chargé au premier usage.")
        self._player_placeholder.setAlignment(QtCore.Qt.AlignCenter)
        self._player_layout.addWidget(self._player_placeholder, 1)
        self.player_tab = tab_player
        self.player_tab_index = self.tabs.addTab(tab_player, "Lecteur")

        # ---- Tab 4: Salon (Quickload)
        self.salon_tab = SalonTab(self, db=self.db, log=self.logln)
        self.salon_tab.quickload_requested.connect(self.on_salon_quickload)
        self.salon_tab.edit_requested.connect(self.on_salon_open_in_editor)
        self.salon_tab_index = self.tabs.addTab(self.salon_tab, "Salon")

        # ---- Tab 5: Configuration (thème/style)
        self._theme_specs = discover_themes()
        self._available_themes = list(self._theme_specs.keys())
        cfg = self._load_user_config()
        initial_theme = cfg.get("theme") if cfg.get("theme") in self._available_themes else (self._available_themes[0] if self._available_themes else "light")
        initial_style = cfg.get("style") if cfg.get("style") else "Fusion"
        initial_epg_path = cfg.get("epg_path") or ""
        self._current_theme = initial_theme
        self._current_style = initial_style
        self._current_epg_path = initial_epg_path
        if initial_epg_path and hasattr(self, "epg_url"):
            self.epg_url.setText(initial_epg_path)
        styles = [s for s in QtWidgets.QStyleFactory.keys() if s.lower() != "windowsvista"]
        if not styles:
            styles = ["Fusion", "Windows"]

        self.settings_tab = SettingsTab(
            self,
            themes=self._available_themes,
            initial_theme=initial_theme,
            styles=styles,
            initial_style=initial_style,
            initial_epg_path=initial_epg_path,
        )
        self.settings_tab.config_preview.connect(self.on_config_preview)
        self.settings_tab.config_changed.connect(self.on_config_changed)
        self.tabs.addTab(self.settings_tab, "Configuration")

        # ---- Tab 7: Info (remerciements + liens utiles)
        info_tab = QtWidgets.QWidget()
        info_layout = QtWidgets.QVBoxLayout(info_tab)

        info_text = QtWidgets.QTextBrowser()
        info_text.setOpenExternalLinks(True)
        info_text.setHtml(
            """
            <h3>Liens GitHub</h3>
            <ul>
              <li><a href="https://github.com/iptv-org/iptv">iptv-org/iptv</a></li>
              <li><a href="https://github.com/iptv-org/api">iptv-org/api</a></li>
              <li><a href="https://github.com/iptv-org/epg">iptv-org/epg</a></li>
            </ul>
            <p>Un grand merci aux créateurs et mainteneurs d'<a href="https://github.com/iptv-org">iptv-org</a> pour leur travail communautaire sur les playlists et l'API.</p>
            """
        )
        info_layout.addWidget(info_text)
        status_box = QtWidgets.QGroupBox("Indicateurs")
        status_form = QtWidgets.QFormLayout(status_box)
        self.info_lbl_vlc = QtWidgets.QLabel("–")
        self.info_lbl_epg = QtWidgets.QLabel("–")
        self.info_lbl_api = QtWidgets.QLabel("–")
        self.info_lbl_md = QtWidgets.QLabel("–")
        status_form.addRow("VLC", self.info_lbl_vlc)
        status_form.addRow("EPG", self.info_lbl_epg)
        status_form.addRow("API iptv-org", self.info_lbl_api)
        status_form.addRow("PLAYLISTS.md", self.info_lbl_md)
        info_layout.addWidget(status_box)

        btn_row = QtWidgets.QHBoxLayout()
        self.btn_info_refresh = QtWidgets.QPushButton("Rafraichir indicateurs")
        btn_row.addWidget(self.btn_info_refresh)
        btn_row.addStretch(1)
        info_layout.addLayout(btn_row)

        info_layout.addStretch(1)
        self.tabs.addTab(info_tab, "Info")
        # Appliquer la config dès le démarrage
        self.on_config_changed({"style": initial_style, "theme": initial_theme, "epg_path": initial_epg_path})

        # ✅ Refresh Qt-safe (au démarrage)
        QtCore.QTimer.singleShot(0, self.salon_tab.refresh)
        QtCore.QTimer.singleShot(0, self.refresh_info_status)

        # ---- Log global (visible pour tous les onglets)
        self.log_wrap = QtWidgets.QFrame()
        log_v = QtWidgets.QVBoxLayout(self.log_wrap)
        log_v.setContentsMargins(4, 0, 4, 0)
        log_v.setSpacing(4)

        log_header = QtWidgets.QHBoxLayout()
        log_v.addLayout(log_header)

        self.btn_toggle_log = QtWidgets.QToolButton(text="Log", checkable=True, checked=True)
        self.btn_toggle_log.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.btn_toggle_log.setArrowType(QtCore.Qt.DownArrow)

        self.cmb_log_level = QtWidgets.QComboBox()
        self.cmb_log_level.addItems(["ALL", "DEBUG", "INFO", "WARN", "ERROR"])
        self.cmb_log_level.setCurrentText("INFO")
        self.btn_log_clear = QtWidgets.QPushButton("Clear")

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()

        log_header.addWidget(self.btn_toggle_log)
        log_header.addStretch(1)
        log_header.addWidget(self.progress, 1)
        log_header.addWidget(QtWidgets.QLabel("Niveau"))
        log_header.addWidget(self.cmb_log_level)
        log_header.addWidget(self.btn_log_clear)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        log_v.addWidget(self.log, 1)

        self.vsplit.addWidget(self.log_wrap)
        self.vsplit.setStretchFactor(0, 3)
        self.vsplit.setStretchFactor(1, 1)
        self.vsplit.setSizes([700, 260])
        self._log_last_size = 260

        self.btn_toggle_log.clicked.connect(self._toggle_log)
        self.btn_log_clear.clicked.connect(self._clear_logs)
        self.cmb_log_level.currentTextChanged.connect(self._on_log_level_changed)
        self.btn_info_refresh.clicked.connect(self.refresh_info_status)

        # Signals
        self.act_import_file.triggered.connect(self.on_load_file)
        self.act_import_url.triggered.connect(self.on_load_url)
        self.btn_test.clicked.connect(self.on_test)
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_send_player.clicked.connect(self.on_send_to_player)
        self.act_export_filtered.triggered.connect(self.on_export)
        self.act_export_salon.triggered.connect(self.on_export_salon)
        self.act_del_dead.triggered.connect(self.on_delete_dead)
        self.act_del_sel.triggered.connect(self.on_delete_selected)
        self.act_merge_file.triggered.connect(self.on_merge_file)
        self.act_merge_url.triggered.connect(self.on_merge_url)
        self.act_merge_salon.triggered.connect(self.on_merge_salon)
        self.act_merge_txt_links.triggered.connect(self.on_merge_txt_links)
        self.act_merge_streams_api.triggered.connect(self.on_merge_streams_api)

        # Debounce filters to keep UI responsive on large playlists.
        self._channels_filter_timer = QtCore.QTimer(self)
        self._channels_filter_timer.setSingleShot(True)
        self._channels_filter_timer.setInterval(150)
        self._channels_filter_timer.timeout.connect(self.apply_filter)
        self.search.textChanged.connect(lambda *_: self._channels_filter_timer.start())

        self.btn_refresh_lists.clicked.connect(self.on_refresh_playlists)
        self.btn_open_streams.clicked.connect(self.on_open_streams_dialog)
        self.btn_load_selected_list.clicked.connect(self.on_load_selected_playlists)
        self.tree.itemSelectionChanged.connect(self.on_tree_selection_changed)
        self.tree.itemClicked.connect(self._tree_click_expand)

        self._tree_filtreer_timer = QtCore.QTimer(self)
        self._tree_filtreer_timer.setSingleShot(True)
        self._tree_filtreer_timer.setInterval(150)
        self._tree_filtreer_timer.timeout.connect(self.apply_tree_filtreer)
        self.list_search.textChanged.connect(lambda *_: self._tree_filtreer_timer.start())

        self.playlists_loaded.connect(self._populate_tree)
        self.playlists_error.connect(self._log_error)
        self.import_merged.connect(self._import_merged)

        # VLC + EPG
        self.table.cellDoubleClicked.connect(self.on_channel_double_clicked)
        self.table.itemSelectionChanged.connect(self.on_channel_selected)
        self.btn_epg_update.clicked.connect(self.on_epg_update)
        self.btn_epg_guide.clicked.connect(self.on_epg_guide)
        self.btn_epg_export.clicked.connect(self.on_epg_export)

        self.epg_ok.connect(self.on_epg_ok)
        self.epg_fail.connect(self.on_epg_fail)
        self.epg_progress.connect(self.on_epg_progress)
        self.epg_progress_value.connect(self.on_epg_progress_value)

        self._log_buffer: deque[tuple[int, str]] = deque(maxlen=3000)  # (level_num, rendered_line)
        self._log_level_min = 20  # INFO
        self.log_sig.connect(self._append_log_line)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._update_export_salon_label()

        self._playlists_index = None
        self._all_tree_items: list[tuple[QtWidgets.QTreeWidgetItem, str]] = []

    @QtCore.Slot(int, str)
    def _append_log_line(self, level_num: int, line: str):
        self._log_buffer.append((int(level_num), line))
        if int(level_num) < int(self._log_level_min):
            return

        self.log.appendPlainText(line)
        try:
            self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)
        except Exception:
            pass

    def logln(self, msg: str, level: str = "INFO"):
        if msg is None:
            return
        level = (level or "INFO").strip().upper()
        level_num = {"DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40}.get(level, 20)
        ts = time.strftime("%H:%M:%S")
        for raw_line in str(msg).splitlines() or [""]:
            line = raw_line.rstrip()
            self.log_sig.emit(level_num, f"[{ts}] {level:<5} {line}")

    def logexc(self, context: str, exc: Exception):
        ctx = (context or "").strip()
        prefix = f"{ctx}: " if ctx else ""
        self.logln(f"{prefix}{type(exc).__name__}: {exc}", level="ERROR")

    def _on_log_level_changed(self, level: str):
        level = (level or "INFO").strip().upper()
        self._log_level_min = {"ALL": 0, "DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}.get(level, 20)
        self._rebuild_log_view()

    def _rebuild_log_view(self):
        self.log.setUpdatesEnabled(False)
        try:
            self.log.clear()
            for level_num, line in self._log_buffer:
                if int(level_num) >= int(self._log_level_min):
                    self.log.appendPlainText(line)
            try:
                self.log.moveCursor(QtGui.QTextCursor.MoveOperation.End)
            except Exception:
                pass
        finally:
            self.log.setUpdatesEnabled(True)

    def _clear_logs(self):
        self._log_buffer.clear()
        self.log.clear()

    def _progress_start(self, maximum: int | None = None):
        try:
            if maximum is None or maximum <= 0:
                self.progress.setRange(0, 0)  # indéterminé
            else:
                self.progress.setRange(0, int(maximum))
                self.progress.setValue(0)
            self.progress.show()
        except Exception:
            pass

    def _progress_update(self, value: int):
        try:
            if self.progress.isHidden():
                return
            self.progress.setRange(self.progress.minimum(), self.progress.maximum())
            self.progress.setValue(int(value))
        except Exception:
            pass

    def _progress_done(self):
        try:
            self.progress.hide()
            self.progress.setValue(0)
        except Exception:
            pass

    def _run_in_background(self, func, *, on_success=None, on_error=None, on_finally=None, desc: str = ""):
        """
        Exécute une fonction potentiellement bloquante (réseau/I/O) dans un thread, et rapatrie les callbacks sur le thread Qt.
        """
        def target():
            try:
                res = func()
                if on_success:
                    QtCore.QTimer.singleShot(0, self, lambda r=res: on_success(r))
            except Exception as e:
                if on_error:
                    QtCore.QTimer.singleShot(0, self, lambda err=e: on_error(err))
                else:
                    self.logexc(desc or "Tâche réseau", e)
            finally:
                if on_finally:
                    QtCore.QTimer.singleShot(0, self, on_finally)

        threading.Thread(target=target, daemon=True).start()

    def _info_set_status(self, label: QtWidgets.QLabel, ok: bool, text: str, detail: str | None = None):
        icon = "✅" if ok else "❌"
        extra = f" ({detail})" if detail else ""
        label.setText(f"{icon} {text}{extra}")

    def _refresh_info_local_status(self):
        # VLC
        vlc_ok = True
        vlc_detail = ""
        try:
            import vlc as _vlc  # noqa: F401
        except Exception as e:
            vlc_ok = False
            vlc_detail = str(e)
        self._info_set_status(self.info_lbl_vlc, vlc_ok, "VLC installé", vlc_detail if not vlc_ok else None)

        # EPG/Node/npm + chemin
        epg_ok = False
        epg_detail_parts: list[str] = []

        try:
            npm_bin = shutil.which("npm") or shutil.which("npm.cmd")
            if not npm_bin:
                epg_detail_parts.append("npm absent")
            elif getattr(self, "_last_epg_xml", None):
                epg_ok = True
                epg_detail_parts.append("EPG chargé en mémoire")
            elif self._current_epg_path:
                p = Path(self._current_epg_path)
                if p.exists():
                    epg_ok = True
                    epg_detail_parts.append(f"Chemin OK: {p}")
                else:
                    epg_detail_parts.append(f"Chemin absent: {p}")
            else:
                epg_detail_parts.append("Chemin EPG non configuré")
        except Exception as e:
            epg_detail_parts.append(str(e))

        epg_detail = "; ".join([d for d in epg_detail_parts if d])
        self._info_set_status(self.info_lbl_epg, epg_ok, "EPG/Node", epg_detail if not epg_ok else None)

    def _refresh_info_remote_status(self):
        self.btn_info_refresh.setEnabled(False)
        self._info_set_status(self.info_lbl_api, False, "API iptv-org", "vérification…")
        self._info_set_status(self.info_lbl_md, False, "PLAYLISTS.md", "vérification…")

        def check():
            api_ok = False
            api_detail = ""
            md_ok = False
            md_detail = ""
            try:
                r = requests.get(f"{PLAYLISTS_API_BASE}/feeds.json", timeout=5)
                api_ok = r.ok
                if not api_ok:
                    api_detail = f"HTTP {r.status_code}"
            except Exception as e:
                api_detail = str(e)

            try:
                r = requests.get(PLAYLISTS_MD_RAW, timeout=5, stream=True)
                md_ok = r.ok
                if not md_ok:
                    md_detail = f"HTTP {r.status_code}"
            except Exception as e:
                md_detail = str(e)

            return (api_ok, api_detail, md_ok, md_detail)

        def on_done(res):
            api_ok, api_detail, md_ok, md_detail = res
            self._info_set_status(self.info_lbl_api, api_ok, "API iptv-org", api_detail if not api_ok else None)
            self._info_set_status(self.info_lbl_md, md_ok, "PLAYLISTS.md", md_detail if not md_ok else None)

        self._run_in_background(
            check,
            on_success=on_done,
            on_finally=lambda: self.btn_info_refresh.setEnabled(True),
            desc="Info tab checks",
        )

    def refresh_info_status(self):
        self._refresh_info_local_status()
        self._refresh_info_remote_status()

    def _toggle_log(self, checked: bool):
        # On masque uniquement la zone texte, on garde l'entête (bouton) visible pour pouvoir réafficher,
        # et on ajuste le splitter pour libérer la place quand le log est masqué.
        try:
            self.log.setVisible(checked)
        except Exception:
            pass
        try:
            sizes = self.vsplit.sizes()
            total = sum(sizes) if sizes else self.vsplit.size().height()
            if not checked:
                if len(sizes) > 1 and sizes[1] > 0:
                    self._log_last_size = sizes[1]
                collapsed = self.btn_toggle_log.sizeHint().height() + 8
                top = max(1, total - collapsed)
                self.vsplit.setSizes([top, collapsed])
            else:
                log_size = max(120, getattr(self, "_log_last_size", 260))
                top = max(1, total - log_size)
                self.vsplit.setSizes([top, log_size])
        except Exception:
            pass
        self.btn_toggle_log.setArrowType(QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)

    def _on_tab_changed(self, idx: int):
        player_idx = self.tabs.indexOf(getattr(self, "player_tab", None))
        channels_idx = self.tabs.indexOf(getattr(self, "channels_tab", None))
        if idx == player_idx and player_idx != -1:  # Lecteur
            self._ensure_player_widget()
        if channels_idx != -1 and idx != channels_idx:  # quitter l'onglet Éditeur -> on oublie le contexte d'édition Salon
            self._reset_editing_context()

    def _ensure_player_widget(self) -> VlcPlayerPanel:
        if self.player_widget is not None:
            return self.player_widget

        try:
            self._player_placeholder.setText("Chargement du lecteur...")
        except Exception:
            pass

        pw = VlcPlayerPanel(
            get_now_next=self.db.get_now_next,
            list_programs=self.db.list_epg_programs,
            log=self.logln,
        )

        # Remplace le placeholder par le lecteur instancié
        while self._player_layout.count():
            item = self._player_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._player_layout.addWidget(pw, 1)

        self.player_widget = pw

        # Si des chaînes/EPG sont déjà chargés, on alimente le lecteur.
        if self.channels:
            try:
                pw.set_channels_from_objects(self.channels)
            except Exception:
                pass
        if self.epg_loaded:
            try:
                pw.set_epg_callbacks(
                    get_now_next=self.db.get_now_next,
                    list_programs=self.db.list_epg_programs,
                )
            except Exception:
                pass

        return pw

    # -------- Settings --------

    def on_theme_changed(self, theme: str):
        """Applique une palette claire/sombre simple sur l'application."""
        app = QtWidgets.QApplication.instance()
        if not app:
            return

        # Style actuel (géré séparément)
        spec = self._theme_specs.get(theme) or next(iter(self._theme_specs.values()))
        pal = spec.palette
        app.setPalette(pal)
        self._current_theme = theme
        self._save_user_config()

    def _apply_config(self, payload: dict, persist: bool):
        if not isinstance(payload, dict):
            return
        app = QtWidgets.QApplication.instance()
        style_name = (payload.get("style") or "").strip()
        theme_name = (payload.get("theme") or "").strip()
        epg_path = (payload.get("epg_path") or "").strip()

        changed_parts = []

        if style_name and app and style_name in QtWidgets.QStyleFactory.keys():
            app.setStyle(style_name)
            self._current_style = style_name
            changed_parts.append(f"style={style_name}")

        if theme_name:
            spec = self._theme_specs.get(theme_name) or next(iter(self._theme_specs.values()), None)
            if spec and app:
                app.setPalette(spec.palette)
            self._current_theme = theme_name
            changed_parts.append(f"theme={theme_name}")

        if epg_path:
            self._current_epg_path = epg_path
            try:
                if hasattr(self, "epg_url"):
                    self.epg_url.setText(epg_path)
            except Exception:
                pass
            changed_parts.append(f"epg_path={epg_path}")
        else:
            self._current_epg_path = ""
            changed_parts.append("epg_path=<vide>")

        if persist:
            self._save_user_config()
            if changed_parts:
                self.logln("Config: enregistrée -> " + ", ".join(changed_parts))
        else:
            if changed_parts:
                self.logln("Config: prévisualisation -> " + ", ".join(changed_parts), level="DEBUG")

    def on_config_preview(self, payload: dict):
        """Applique sans persister (prévisualisation)."""
        self._apply_config(payload, persist=False)

    def on_config_changed(self, payload: dict):
        """Applique et enregistre la configuration."""
        self._apply_config(payload, persist=True)

    # -------- Salon --------

    def on_salon_quickload(self, playlist_id: int):
        try:
            rows = self.db.get_channels(int(playlist_id))
        except Exception as e:
            self.logln(f"Salon: erreur DB: {e}")
            return

        rec = None
        try:
            rec = next((p for p in self.db.list_playlists() if int(p.id) == int(playlist_id)), None)
            if rec and getattr(self, "epg_url", None) is not None:
                self.epg_url.setText(rec.epg_url or "")
        except Exception:
            rec = None

        channels = self._channels_from_db_rows(rows)
        if not channels:
            self.logln("Salon: playlist vide.")
            return

        self._log_risk_overview(channels)
        player = self._ensure_player_widget()
        try:
            player.set_channels_from_objects(channels)
        except Exception as e:
            self.logln(f"Salon: erreur player: {e}")
            return

        self.logln(f"Salon: charge dans le player ({len(channels)} chaines).")
        # Aller sur l'onglet Lecteur si présent, sinon fallback à l'index 0
        target_idx = getattr(self, "player_tab_index", None)
        if target_idx is None or target_idx < 0:
            target_idx = 0
        self.tabs.setCurrentIndex(int(target_idx))
        self._maybe_auto_epg_for_salon(rec.epg_url if rec else "", playlist_id)

    def on_salon_open_in_editor(self, playlist_id: int):
        try:
            rows = self.db.get_channels(int(playlist_id))
        except Exception as e:
            self.logln(f"Salon: erreur DB: {e}")
            return

        rec = None
        try:
            rec = next((p for p in self.db.list_playlists() if int(p.id) == int(playlist_id)), None)
        except Exception:
            rec = None

        self._editing_playlist_id = int(playlist_id)
        self._editing_playlist_name = rec.name if rec else None
        if rec and rec.url:
            self._last_import_source = rec.url
        if rec and getattr(self, "epg_url", None) is not None:
            self.epg_url.setText(rec.epg_url or "")
        self._update_export_salon_label()

        channels = self._channels_from_db_rows(rows)
        if not channels:
            self.logln("Salon: playlist vide.")
            return

        self._log_risk_overview(channels)
        self.channels = channels
        self.refresh_table(self.channels)
        self.search.clear()
        self.logln(f"Editeur: playlist Salon chargee ({len(channels)} chaines).")

        # Aller sur l'onglet Chaînes (éditeur)
        self.tabs.setCurrentIndex(1)
        self._maybe_auto_epg_for_salon(rec.epg_url if rec else "", playlist_id)

    # -------- VLC --------

    def on_channel_double_clicked(self, row: int, col: int):
        item = self.table.item(row, 6)  # URL
        if not item:
            return
        url = item.text().strip()
        if not url:
            return

        self.logln(f"Lecture: {url}")
        player = self._ensure_player_widget()
        player.play_url(url)
        target_idx = getattr(self, "player_tab_index", None)
        if target_idx is None or target_idx < 0:
            target_idx = 0
        self.tabs.setCurrentIndex(int(target_idx))

    # -------- EPG --------

    def on_epg_update(self):
        raw = self.epg_url.text().strip()
        if not raw:
            self.logln("EPG: URL/dossier vide.")
            return

        self.btn_epg_update.setEnabled(False)
        self.btn_epg_guide.setEnabled(False)
        self.epg_progress.emit("EPG: preparation...")
        self.epg_progress_value.emit(-1)  # indéterminé le temps de l'analyse
        cache_key = self._epg_cache_key()

        def run():
            try:
                self.epg_progress.emit("EPG: analyse de la cible...")
                p = Path(raw)

                # If the target is a local npm repo, generate XMLTV locally; otherwise download the remote feed.
                if p.exists() and p.is_dir() and (p / "package.json").exists():
                    msg = f"EPG: generation via repo npm: {p}"
                    self.logln(msg)
                    self.epg_progress.emit(msg)

                    tvg_ids = [c.tvg_id for c in self.channels if (c.tvg_id or "").strip()]
                    xml = generate_xmltv_for_tvg_ids(
                        repo=p,
                        tvg_ids=tvg_ids,
                        days=1,
                        timeout_s=900,
                        log=self.logln,
                    )
                else:
                    msg = f"EPG: telechargement + import: {raw}"
                    self.logln(msg)
                    self.epg_progress.emit(msg)
                    def _progress(read: int, total: int):
                        if total > 0:
                            pct = int((read / total) * 100)
                            self.epg_progress_value.emit(pct)
                        else:
                            self.epg_progress_value.emit(-1)

                    xml = download_xmltv(raw, progress_cb=_progress)
                    self.epg_progress_value.emit(100)

                programs = list(iter_programs(xml))
                QtCore.QTimer.singleShot(0, self, lambda: self._load_epg_snapshot(xml, programs, cache_key))

            except Exception as e:
                self.epg_fail.emit(str(e))

        threading.Thread(target=run, daemon=True).start()

    @QtCore.Slot(str)
    def on_epg_progress(self, msg: str):
        self.lbl_epg_status.setText(msg or "")

    @QtCore.Slot(int)
    def on_epg_progress_value(self, val: int):
        if val is None:
            return
        if val < 0:
            self._progress_start(None)
        else:
            self._progress_start(100)
            self._progress_update(max(0, min(100, int(val))))

    @QtCore.Slot()
    def on_epg_ok(self):
        self.btn_epg_update.setEnabled(True)
        self.btn_epg_guide.setEnabled(True)
        self.logln("EPG: import OK.")
        if self._last_epg_coverage:
            self.lbl_epg_status.setText(self._last_epg_coverage)
        else:
            self.lbl_epg_status.setText("EPG: import OK.")
        self._progress_done()
        if self.player_widget is not None:
            try:
                self.player_widget.set_epg_callbacks(
                    get_now_next=self.db.get_now_next,
                    list_programs=self.db.list_epg_programs,
                )
            except Exception:
                pass
        self.on_channel_selected()

    @QtCore.Slot(str)
    def on_epg_fail(self, err: str):
        self.btn_epg_update.setEnabled(True)
        self.btn_epg_guide.setEnabled(False)
        self.logln(f"EPG: erreur import: {err}", level="ERROR")
        self.lbl_epg_status.setText(f"EPG: erreur: {err}")
        self._progress_done()

    def on_epg_export(self):
        if not self._last_epg_xml:
            self.logln("EPG: rien a exporter (pas de snapshot).")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Exporter EPG (snapshot)", "epg_snapshot.xml", "XMLTV (*.xml);;Tous (*.*)"
        )
        if not path:
            return
        try:
            Path(path).write_bytes(self._last_epg_xml)
            self.logln(f"EPG: snapshot exporte -> {path}")
        except Exception as e:
            self.logln(f"EPG: erreur Exporte: {e}", level="ERROR")

    def on_epg_guide(self):
        sel = self.table.selectionModel().selectedRows()
        if not sel:
            self.logln("EPG: sélectionne une chaîne.")
            return

        row = sel[0].row()
        name_item = self.table.item(row, 0)
        tvg_item = self.table.item(row, 2)

        ch_name = (name_item.text() if name_item else "").strip()
        tvg_id = (tvg_item.text() if tvg_item else "").strip()

        if not tvg_id:
            self.logln("EPG: (pas de tvg-id) → impossible d'ouvrir le guide.")
            return

        if not self.epg_loaded:
            self.logln("EPG: pas chargé.")
            return

        dlg = EpgDialog(self, self.db, tvg_id, ch_name or tvg_id)
        dlg.exec()

    def on_channel_selected(self):
        sel = self.table.selectionModel().selectedRows()
        if not sel:
            return

        row = sel[0].row()
        tvg_item = self.table.item(row, 2)
        if not tvg_item:
            return

        tvg_id = (tvg_item.text() or "").strip()
        if not tvg_id:
            return

        if not self.epg_loaded:
            return

        now_ts = int(time.time())
        nowp, nextp = self.db.get_now_next(tvg_id, now_ts)

        # Si besoin d'afficher now/next, réintroduire des labels ou une boîte de dialogue dédiée.
        # Ici on ne met plus à jour d'UI.

    # -------- close --------

    def closeEvent(self, event):
        if self._probe_worker is not None:
            try:
                self._probe_worker.stop()
            except Exception:
                pass

        if self._probe_thread is not None:
            try:
                self._probe_thread.quit()
                self._probe_thread.wait(3000)
            except Exception:
                pass

        try:
            if getattr(self, "player_widget", None) is not None:
                self.player_widget.shutdown()
        except Exception:
            pass

        super().closeEvent(event)

    # -------- Channels UI --------

    def refresh_table(self, data: list[Channel] | None = None, *, resize_columns: bool = True):
        data = data if data is not None else self.channels

        was_sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(data))
            for row, ch in enumerate(data):
                name_txt = ch.name
                opts = getattr(ch, "vlc_opts", []) or []
                if opts:
                    name_txt = (name_txt or "") + " [OPT]"
                name_item = QtWidgets.QTableWidgetItem(name_txt)
                if opts:
                    name_item.setToolTip("Options VLC:\n" + "\n".join(str(o) for o in opts))
                self.table.setItem(row, 0, name_item)
                self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(ch.group))
                self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(ch.tvg_id))
                risk_txt = f"{ch.risk_badge} {ch.risk_level} ({int(round(ch.risk_score))}/100)"
                risk_item = QtWidgets.QTableWidgetItem(risk_txt)
                if ch.risk_reasons:
                    risk_item.setToolTip(ch.risk_reasons)
                self.table.setItem(row, 3, risk_item)
                self.table.setItem(row, 4, QtWidgets.QTableWidgetItem(ch.risk_reasons))
                self.table.setItem(row, 5, QtWidgets.QTableWidgetItem(ch.status))
                self.table.setItem(row, 6, QtWidgets.QTableWidgetItem(ch.url))
        finally:
            self.table.setUpdatesEnabled(True)
            self.table.setSortingEnabled(was_sorting)

        if resize_columns:
            self.table.resizeColumnsToContents()

    def get_filtered_channels(self) -> list[Channel]:
        q = self.search.text().strip().lower()
        if not q:
            return list(self.channels)
        out = []
        for ch in self.channels:
            hay = f"{ch.name} {ch.group} {ch.tvg_id} {ch.status} {ch.url} {ch.risk_level} {ch.risk_reasons} {ch.risk_score}".lower()
            if q in hay:
                out.append(ch)
        return out

    def apply_filter(self):
        q = self.search.text().strip().lower()
        filtered = self.get_filtered_channels()
        self.refresh_table(filtered, resize_columns=not q)

    def _log_risk_overview(self, channels: list[Channel]):
        # Compute risk badges for the current set and log a compact summary.
        assessments = score_channels(channels)
        if not assessments:
            return assessments

        counts = {"🟢": 0, "🟡": 0, "🔴": 0}
        for a in assessments:
            if a.badge in counts:
                counts[a.badge] += 1

        self.logln(
            f"Risque (indicatif, informatif uniquement): {counts['🔴']} 🔴 / {counts['🟡']} 🟡 / {counts['🟢']} 🟢"
        )
        return assessments

    def _reset_editing_context(self):
        self._editing_playlist_id = None
        self._editing_playlist_name = None
        self._update_export_salon_label()

    def _update_export_salon_label(self):
        if self._editing_playlist_id is not None and self._editing_playlist_name:
            self.act_export_salon.setText(f"Enregistrer '{self._editing_playlist_name}'")
        else:
            self.act_export_salon.setText("Exporter au Salon")

    # -------- EPG helpers --------
    def _epg_cache_key(self) -> str | None:
        if self._editing_playlist_id is not None:
            return f"playlist_{int(self._editing_playlist_id)}"
        raw = self.epg_url.text().strip() if hasattr(self, "epg_url") else ""
        if raw:
            return f"url_{abs(hash(raw))}"
        return None

    def _epg_cache_path(self, key: str) -> Path:
        return self._epg_cache_dir / f"{key}.xml"

    def _load_epg_snapshot(self, xml_bytes: bytes, programs: list[dict], cache_key: str | None):
        try:
            self.epg_progress.emit(f"EPG: insertion snapshot ({len(programs)} programmes)...")
            self.db.clear_epg()
            self.db.upsert_epg_programs(programs)
            self.epg_loaded = True
            self._last_epg_xml = xml_bytes

            if cache_key:
                try:
                    self._epg_cache_dir.mkdir(parents=True, exist_ok=True)
                    self._epg_cache_path(cache_key).write_bytes(xml_bytes)
                except Exception:
                    pass

            tvg_in_epg = {p.get("tvg_id", "") for p in programs}
            total_with_id = sum(1 for c in self.channels if (c.tvg_id or "").strip())
            matched = sum(1 for c in self.channels if (c.tvg_id or "").strip() in tvg_in_epg)
            coverage_txt = f"EPG: couverture {matched}/{total_with_id} tvg-id" if total_with_id else "EPG: aucune tvg-id"
            self._last_epg_coverage = coverage_txt
            self.epg_progress.emit(coverage_txt)
            self.epg_progress.emit("EPG: import termine.")
            self.epg_ok.emit()
        except Exception as e:
            self.epg_fail.emit(str(e))

    def _try_load_epg_cache(self, epg_url: str, playlist_id: int | None = None) -> bool:
        key = f"playlist_{int(playlist_id)}" if playlist_id is not None else (f"url_{abs(hash(epg_url))}" if epg_url else None)
        if not key:
            return False
        path = self._epg_cache_path(key)
        if not path.exists():
            return False
        try:
            age_h = (time.time() - path.stat().st_mtime) / 3600.0
            if age_h > float(self._epg_cache_ttl_hours):
                return False
            xml = path.read_bytes()
            programs = list(iter_programs(xml))
            self._load_epg_snapshot(xml, programs, key)
            self.logln(f"EPG: cache charge ({path.name}, age {age_h:.1f}h).")
            return True
        except Exception as e:
            self.logln(f"EPG: cache invalide ({e}).")
            return False

    def _maybe_auto_epg_for_salon(self, epg_url: str, playlist_id: int | None):
        if not epg_url or not self.chk_epg_auto.isChecked():
            return
        if self._try_load_epg_cache(epg_url, playlist_id):
            return
        # Pas de cache frais: lancer un update si URL connue
        self.on_epg_update()

    def import_m3u_text(self, text: str, label: str = ""):
        # Toute importation "fraîche" invalide un contexte d'édition Salon
        self._reset_editing_context()

        source_label = (label or "").strip() or "Import local"
        self._last_import_source = source_label

        self.channels = parse_m3u(text)
        self._log_risk_overview(self.channels)
        self.logln(f"Importé: {len(self.channels)} chaînes ({source_label})")
        self.apply_filter()
        self.tabs.setCurrentIndex(1)


    def _merge_channels(self, new_channels: list[Channel], source_label: str):
        if new_channels is None:
            return
        existing_by_url: dict[str, Channel] = {}
        merged: list[Channel] = []

        for c in self.channels:
            key = (c.url or "").strip()
            if key and key not in existing_by_url:
                existing_by_url[key] = c
                merged.append(c)

        added = 0
        for c in new_channels:
            key = (c.url or "").strip()
            if not key or key in existing_by_url:
                continue
            existing_by_url[key] = c
            merged.append(c)
            added += 1

        if added == 0:
            self.logln(f"Fusion: aucune nouvelle chaine (source: {source_label}).")
            return

        self.channels = merged
        self._log_risk_overview(self.channels)
        self.apply_filter()
        self.logln(f"Fusion: +{added}, total {len(self.channels)} (source: {source_label}).")

    def _channels_from_db_rows(self, rows: list[dict]) -> list[Channel]:
        out = []
        for r in rows:
            out.append(
                Channel(
                    extinf=r.get("extinf", ""),
                    url=r.get("url", ""),
                    name=r.get("name", ""),
                    group=r.get("group", ""),
                    tvg_id=r.get("tvg_id", ""),
                    vlc_opts=r.get("vlc_opts", []) or [],
                    status="-",
                )
            )
        return out

    def on_load_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Choisir une playlist", "", "M3U (*.m3u *.m3u8);;Tous (*.*)"
        )
        if not path:
            return
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
        self.import_m3u_text(text, Path(path).name)

    def on_load_url(self):
        url, ok = QtWidgets.QInputDialog.getText(
            self, "Importer depuis une URL", "Colle l'URL .m3u/.m3u8 :"
        )
        if not ok or not url.strip():
            return
        url = url.strip()
        self.act_import_url.setEnabled(False)
        self.logln(f"Telechargement: {url}")
        self._progress_start()

        self._run_in_background(
            lambda: requests.get(url, timeout=20).text,
            on_success=lambda text: self.import_merged.emit(text, url),
            on_error=lambda e: self.logexc("Erreur telechargement", e),
            on_finally=lambda: (self.act_import_url.setEnabled(True), self._progress_done()),
            desc="Import URL",
        )
    def on_merge_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Fusionner playlist", "", "M3U (*.m3u *.m3u8);;Tous (*.*)"
        )
        if not path:
            return
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
        new_channels = parse_m3u(text)
        self._merge_channels(new_channels, Path(path).name)

    def on_merge_url(self):
        url, ok = QtWidgets.QInputDialog.getText(
            self, "Fusionner depuis une URL", "Colle l'URL .m3u/.m3u8 :"
        )
        if not ok or not url.strip():
            return
        url = url.strip()
        self.logln(f"Fusion: telechargement {url}")
        self._progress_start()

        self._run_in_background(
            lambda: parse_m3u(requests.get(url, timeout=20).text),
            on_success=lambda new_channels: self._merge_channels(new_channels, url),
            on_error=lambda e: self.logexc("Erreur fusion URL", e),
            on_finally=lambda: self._progress_done(),
            desc="Fusion URL",
        )

    def on_merge_salon(self):
        try:
            playlists = self.db.list_playlists()
        except Exception as e:
            self.logln(f"Salon: erreur DB: {e}")
            return
        if not playlists:
            self.logln("Salon: aucune playlist disponible.")
            return
        labels = [f"#{p.id} - {p.name}" for p in playlists]
        choice, ok = QtWidgets.QInputDialog.getItem(
            self, "Fusionner playlist du Salon", "Choisis une playlist :", labels, 0, False
        )
        if not ok or not choice:
            return
        try:
            pid = int(choice.split()[0].lstrip("#"))
        except Exception:
            self.logln("Salon: choix invalide.")
            return
        try:
            rows = self.db.get_channels(pid)
        except Exception as e:
            self.logln(f"Salon: erreur lecture playlist: {e}")
            return
        new_channels = self._channels_from_db_rows(rows)
        self._merge_channels(new_channels, f"Salon #{pid}")

    def on_merge_txt_links(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Fichier TXT (liens m3u/m3u8)", "", "Texte (*.txt);;Tous (*.*)"
        )
        if not path:
            return
        urls = []
        for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
            u = line.strip()
            if not u.lower().startswith("http"):
                continue
            if not (u.lower().endswith(".m3u") or u.lower().endswith(".m3u8")):
                continue
            if u not in urls:
                urls.append(u)
        if not urls:
            self.logln("Fusion TXT: aucun lien m3u/m3u8 detecte.")
            return
        self.logln(f"Fusion TXT: telechargement {len(urls)} playlist(s)...")
        self._progress_start(len(urls))

        def fetch_all():
            merged_channels: list[Channel] = []
            for i, u in enumerate(urls, 1):
                try:
                    t = requests.get(u, timeout=25).text
                    merged_channels.extend(parse_m3u(t))
                except Exception as e:
                    self.logln(f"Fusion TXT: KO {u}: {e}")
                self._progress_update(i)
            return merged_channels

        self._run_in_background(
            fetch_all,
            on_success=lambda merged_channels: self._merge_channels(merged_channels, Path(path).name) if merged_channels else self.logln("Fusion TXT: rien a fusionner."),
            on_finally=lambda: self._progress_done(),
            desc="Fusion TXT liens",
        )


    def on_merge_streams_api(self):
        self.streams_widget.set_import_mode("merge")
        self.streams_widget.ensure_loaded()
        self.tabs.setCurrentIndex(self.streams_tab_index)

    def on_test(self):
        if not self.channels:
            self.logln("Aucune playlist chargee.")
            return
        if self._probe_thread:
            self.logln("Test deja en cours.")
            return

        self.logln("Debut test URLs...")
        self._probe_done = 0
        self._probe_total = len(self.channels)
        if self._probe_total:
            self.lbl_probe_status.setText(f"Test URLs: 0/{self._probe_total}")
        else:
            self.lbl_probe_status.clear()
        self.btn_test.setEnabled(False)
        self.btn_stop.setEnabled(True)

        self._probe_thread = QtCore.QThread(self)
        self._probe_worker = ProbeWorker(self.channels, timeout_s=4.0)
        self._probe_worker.moveToThread(self._probe_thread)

        self._probe_thread.started.connect(self._probe_worker.run)
        self._probe_worker.progress.connect(self.on_probe_progress)
        self._probe_worker.progress_count.connect(self.on_probe_progress_count)
        self._probe_worker.finished.connect(self.on_probe_finished)
        self._probe_worker.finished.connect(self._probe_thread.quit)
        self._probe_worker.finished.connect(self._probe_worker.deleteLater)

        self.search.setEnabled(False)
        self.table.setUpdatesEnabled(False)
        self.refresh_table(resize_columns=False)
        self.table.resizeColumnsToContents()
        self.table.setUpdatesEnabled(True)

        self._probe_thread.start()

    def on_export_salon(self):
        data = self.get_filtered_channels()
        if not data:
            if self._editing_playlist_id is not None:
                if QtWidgets.QMessageBox.question(
                    self,
                    "Playlist vide",
                    "La playlist courante est vide.\nSupprimer l'entrée du Salon ?",
                ) == QtWidgets.QMessageBox.Yes:
                    try:
                        self.db.delete_playlist(int(self._editing_playlist_id))
                        self.logln("Salon: playlist supprimée (vide).")
                        self._reset_editing_context()
                        try:
                            self.salon_tab.refresh()
                        except Exception:
                            pass
                    except Exception as e:
                        self.logln(f"Salon: erreur suppression playlist: {e}")
                return
            self.logln("Salon: rien à exporter (liste vide/filtree vide).")
            return

        # Nom pré-rempli: si édition d'une playlist Salon, on propose le nom existant.
        default_name = self._editing_playlist_name or ""
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Exporter au Salon", "Nom de la playlist (Salon) :", text=default_name
        )
        if not ok or not name.strip():
            return
        name = name.strip()

        url = self._last_import_source or "-"
        epg_url_text = self.epg_url.text().strip() if hasattr(self, "epg_url") else ""

        # Détecter doublon de nom pour proposer Remplacer/Dupliquer
        try:
            playlists = self.db.list_playlists()
        except Exception:
            playlists = []
        same_name = [p for p in playlists if (p.name or "").strip().lower() == name.lower()]

        is_update = self._editing_playlist_id is not None
        target_pid: int | None = int(self._editing_playlist_id) if is_update else None

        def prompt_replace_or_duplicate(existing_name: str) -> str:
            msg = QtWidgets.QMessageBox(self)
            msg.setWindowTitle("Playlist existante")
            msg.setText(f"Une playlist nommée '{existing_name}' existe déjà.\nQue faire ?")
            btn_replace = msg.addButton("Remplacer", QtWidgets.QMessageBox.ButtonRole.YesRole)
            btn_dup = msg.addButton("Dupliquer", QtWidgets.QMessageBox.ButtonRole.NoRole)
            btn_cancel = msg.addButton("Annuler", QtWidgets.QMessageBox.ButtonRole.RejectRole)
            msg.exec()
            clicked = msg.clickedButton()
            if clicked == btn_replace:
                return "replace"
            if clicked == btn_dup:
                return "duplicate"
            return "cancel"

        if is_update:
            conflict = None
            for p in same_name:
                if int(p.id) != int(self._editing_playlist_id):
                    conflict = p
                    break
            if conflict:
                choice = prompt_replace_or_duplicate(conflict.name)
                if choice == "cancel":
                    return
                if choice == "replace":
                    target_pid = int(conflict.id)
                else:  # duplicate
                    target_pid = None
                    is_update = False
            else:
                target_pid = int(self._editing_playlist_id)
        else:
            if same_name:
                choice = prompt_replace_or_duplicate(same_name[0].name)
                if choice == "cancel":
                    return
                if choice == "replace":
                    target_pid = int(same_name[0].id)
                    is_update = True
                else:
                    target_pid = None
                    is_update = False

        if is_update and target_pid is not None:
            pid = int(target_pid)
            try:
                self.db.update_playlist(pid, name, url, epg_url_text)
            except Exception as e:
                self.logln(f"Salon: erreur mise à jour playlist: {e}")
                return
        else:
            pid = self.db.add_playlist(name, url, epg_url_text)
            self._editing_playlist_id = pid
            self._editing_playlist_name = name

        payload = [
            {
                "name": c.name,
                "group": c.group,
                "tvg_id": c.tvg_id,
                "url": c.url,
                "extinf": c.extinf,
                "vlc_opts": getattr(c, "vlc_opts", []) or [],
            }
            for c in data
        ]
        self.db.replace_channels(pid, payload)

        action = "mis à jour" if is_update else "exporté"
        self.logln(f"Salon: {action} -> '{name}' ({len(payload)} chaînes).")
        self._editing_playlist_id = pid
        self._editing_playlist_name = name
        self._update_export_salon_label()
        try:
            self.salon_tab.refresh()
        except Exception:
            pass

    @QtCore.Slot(int, int)
    def on_probe_progress_count(self, done: int, total: int):
        self._probe_done = done
        self._probe_total = total
        self.lbl_probe_status.setText(f"Test URLs: {done}/{total}")

    @QtCore.Slot(int, str)
    def on_probe_progress(self, row: int, status: str):
        self.channels[row].status = status
        item = self.table.item(row, 5)
        if item is None:
            item = QtWidgets.QTableWidgetItem(status)
            self.table.setItem(row, 5, item)
        else:
            item.setText(status)

    @QtCore.Slot()
    def on_probe_finished(self):
        self.logln("Test termine.")
        self.btn_test.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.search.setEnabled(True)
        if self._probe_total:
            self.lbl_probe_status.setText(f"Test URLs: termine ({self._probe_done}/{self._probe_total})")
        else:
            self.lbl_probe_status.clear()

        if self._probe_thread is not None:
            self._probe_thread.quit()
            self._probe_thread.wait(2000)

        self._probe_thread = None
        self._probe_worker = None

    def on_stop(self):
        if self._probe_worker:
            self._probe_worker.stop()
            self.logln("Stop demande.")
            self.lbl_probe_status.setText("Test URLs: stop demande")

    def on_delete_dead(self):
        before = len(self.channels)
        self.channels = [c for c in self.channels if not c.status.startswith("KO")]
        self.logln(f"Supprimé KO: {before - len(self.channels)}")
        self.apply_filter()

    def on_delete_selected(self):
        sel = self.table.selectionModel().selectedRows()
        if not sel:
            return

        selected_keys = set()
        for idx in sel:
            r = idx.row()
            name = self.table.item(r, 0).text()
            url = self.table.item(r, 6).text()
            selected_keys.add((name, url))

        before = len(self.channels)
        self.channels = [c for c in self.channels if (c.name, c.url) not in selected_keys]
        self.logln(f"Supprime selection: {before - len(self.channels)}")
        self.apply_filter()

    def on_export(self):
        if not self.channels:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Exporter playlist filtree", "playlist.filtered.m3u", "M3U (*.m3u)"
        )
        if not path:
            return
        write_m3u(self.channels, Path(path))
        self.logln(f"Exporte: {path}")

    def on_send_to_player(self):
        if not self.channels:
            self.logln("Lecteur: aucune playlist chargee.")
            return
        player = self._ensure_player_widget()
        try:
            player.set_channels_from_objects(self.channels)
            self.logln(f"Lecteur: playlist chargee ({len(self.channels)} chaines).")
            target_idx = getattr(self, "player_tab_index", None)
            if target_idx is None or target_idx < 0:
                target_idx = 0
            self.tabs.setCurrentIndex(int(target_idx))
        except Exception as e:
            self.logln(f"Lecteur: erreur chargement playlist: {e}")
            return

    def _tree_click_expand(self, item, column):
        if not item.text(1).strip():
            item.setExpanded(not item.isExpanded())

    def on_tree_selection_changed(self):
        self.btn_load_selected_list.setEnabled(len(self.tree.selectedItems()) > 0)

    def on_open_streams_dialog(self):
        self.streams_widget.set_import_mode("replace")
        self.streams_widget.ensure_loaded()
        self.tabs.setCurrentIndex(self.streams_tab_index)

    def on_refresh_playlists(self):
        self.btn_refresh_lists.setEnabled(False)
        self.btn_load_selected_list.setEnabled(False)
        self.tree.clear()
        self._all_tree_items.clear()
        self.logln("Récupération playlists (api iptv-org, fallback PLAYLISTS.md)…")
        self._progress_start()

        self._run_in_background(
            fetch_playlists_index,
            on_success=lambda idx: self.playlists_loaded.emit(idx),
            on_error=lambda e: self.playlists_error.emit(str(e)),
            on_finally=lambda: self._progress_done(),
            desc="Playlists index",
        )

    @QtCore.Slot(str)
    def _log_error(self, err: str):
        self.logln(f"Erreur: {err}", level="ERROR")
        self.btn_refresh_lists.setEnabled(True)

    @QtCore.Slot(dict)
    def _populate_tree(self, idx: dict):
        self._playlists_index = idx
        self.tree.clear()
        self._all_tree_items.clear()

        src = (idx.get("__source__") or "").strip().lower()
        if src == "api":
            self.logln("Source playlists: API iptv-org (feeds.json).")
        elif src == "md":
            self.logln("Source playlists: fallback PLAYLISTS.md.")

        def add_bucket(title: str, items: list[tuple[str, str]]):
            parent = QtWidgets.QTreeWidgetItem([title, ""])
            parent.setExpanded(False)
            self.tree.addTopLevelItem(parent)
            for name, url in items:
                child = QtWidgets.QTreeWidgetItem([name, url])
                parent.addChild(child)
                hay = f"{title} {name} {url}".lower()
                self._all_tree_items.append((child, hay))

        add_bucket("Category", idx.get("Category", []))
        add_bucket("Language", idx.get("Language", []))
        add_bucket("Country", idx.get("Country", []))
        if idx.get("Subdivision/City"):
            add_bucket("Subdivision/City", idx.get("Subdivision/City", []))

        self.tree.resizeColumnToContents(0)
        self.tree.resizeColumnToContents(1)
        try:
            self.tree.sortItems(0, QtCore.Qt.SortOrder.AscendingOrder)
        except Exception:
            pass
        self.btn_refresh_lists.setEnabled(True)
        self.logln("OK: playlists chargées. Déplie Category/Language/Country puis sélectionne → « Charger la sélection ».")

    def apply_tree_filtreer(self):
        q = self.list_search.text().strip().lower()
        if not q:
            for item, _ in self._all_tree_items:
                item.setHidden(False)
            return
        for item, hay in self._all_tree_items:
            item.setHidden(q not in hay)

    def on_load_selected_playlists(self):
        selected = self.tree.selectedItems()
        if not selected:
            return

        def collect_urls(item: QtWidgets.QTreeWidgetItem) -> list[str]:
            # Recursively walk the tree to gather playlist URLs from any selected node.
            urls = []
            u = item.text(1).strip()
            if u.startswith("http"):
                urls.append(u)
            for i in range(item.childCount()):
                urls.extend(collect_urls(item.child(i)))
            return urls

        urls = []
        for it in selected:
            urls.extend(collect_urls(it))

        seen = set()
        urls_unique = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                urls_unique.append(u)

        if not urls_unique:
            self.logln("Aucune URL détectée. Sélectionne une feuille (URL) ou un parent (Category/Language/Country).")
            return

        self.logln(f"Chargement {len(urls_unique)} playlist(s)…")
        self._progress_start(len(urls_unique))

        def fetch_and_merge():
            merged_texts = []
            for i, url in enumerate(urls_unique, 1):
                try:
                    t = requests.get(url, timeout=25).text
                    merged_texts.append(t)
                except Exception as e:
                    merged_texts.append("")
                    self.playlists_error.emit(f"KO {url}: {e}")
                self._progress_update(i)

            # Merge simple: concatène toutes les lignes non vides (en gardant un seul EXTM3U).
            out = ["#EXTM3U"]
            for t in merged_texts:
                for line in t.splitlines():
                    if line.strip() and line.strip() != "#EXTM3U":
                        out.append(line.rstrip())
            final = "\n".join(out) + "\n"

            label = ", ".join(urls_unique[:3]) + ("…" if len(urls_unique) > 3 else "")
            return final, label

        self._run_in_background(
            fetch_and_merge,
            on_success=lambda res: self.import_merged.emit(res[0], res[1]),
            on_finally=lambda: self._progress_done(),
            desc="Chargement playlists sélectionnées",
        )

    @QtCore.Slot(str, str)
    def _import_merged(self, text: str, label: str):
        self.import_m3u_text(text, label)

    # -------------------------
    # Config persistante
    # -------------------------
    def _load_user_config(self) -> dict:
        try:
            if self.config_path.exists():
                return json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return {}

    def _save_user_config(self):
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "theme": self._current_theme,
                "style": self._current_style,
                "epg_path": self._current_epg_path,
            }
            self.config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass


