# ui/main_window.py
from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
from PySide6 import QtCore, QtWidgets, QtGui

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
# PLAYLISTS.md browser (HTML-aware)
# =========================

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


def fetch_playlists_index(timeout=15) -> dict:
    """Download iptv-org/PLAYLISTS.md and bucket playlist URLs by section names."""
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

    log_sig = QtCore.Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("IPTV Cleaner (PySide6)")
        self.resize(1200, 720)

        self.config_path = Path("data/config.json")
        self._current_style = "Fusion"
        self._current_theme = "light"

        self.channels: list[Channel] = []
        self._probe_thread: QtCore.QThread | None = None
        self._probe_worker: ProbeWorker | None = None

        # DB
        self.db = Storage("data/iptv.db")
        self.epg_loaded = False

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # Splitter vertical: Tabs (haut) + Log (bas)
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

        self.btn_refresh_lists = QtWidgets.QPushButton("Lister playlists (GitHub)")
        self.btn_load_selected_list = QtWidgets.QPushButton("Charger la sélection")
        self.btn_load_selected_list.setEnabled(False)

        self.list_search = QtWidgets.QLineEdit()
        self.list_search.setPlaceholderText("Rechercher (ex: french, canada, sports)…")

        hb.addWidget(self.btn_refresh_lists)
        hb.addWidget(self.btn_load_selected_list)
        hb.addWidget(self.list_search, 1)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Playlist", "URL"])
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setColumnWidth(0, 420)
        self.tree.setRootIsDecorated(True)
        self.tree.setExpandsOnDoubleClick(True)

        vb.addWidget(self.tree, 1)
        self.tabs.addTab(tab_browser, "Playlists (GitHub)")

        # ---- Tab 2: Channels (éditeur playlist)
        tab_channels = QtWidgets.QWidget()
        vc = QtWidgets.QVBoxLayout(tab_channels)

        # Actions (les boutons que tu voulais DANS l'éditeur)
        actions = QtWidgets.QHBoxLayout()
        vc.addLayout(actions)

        self.btn_load = QtWidgets.QPushButton("Importer M3U (fichier)…")
        self.btn_load_url = QtWidgets.QPushButton("Importer M3U (URL)…")
        self.btn_test = QtWidgets.QPushButton("Tester URLs")
        self.btn_stop = QtWidgets.QPushButton("Stop")
        self.btn_export = QtWidgets.QPushButton("Exporter filtré…")
        self.btn_export_salon = QtWidgets.QPushButton("Exporter au Salon")
        self.btn_del_dead = QtWidgets.QPushButton("Supprimer KO")
        self.btn_del_sel = QtWidgets.QPushButton("Supprimer sélection")

        self.btn_stop.setEnabled(False)

        actions.addWidget(self.btn_load)
        actions.addWidget(self.btn_load_url)
        actions.addWidget(self.btn_test)
        actions.addWidget(self.btn_stop)
        actions.addSpacing(12)
        actions.addWidget(self.btn_del_dead)
        actions.addWidget(self.btn_del_sel)
        actions.addWidget(self.btn_export)
        actions.addWidget(self.btn_export_salon)
        actions.addStretch(1)

        # Filtre
        filt = QtWidgets.QHBoxLayout()
        vc.addLayout(filt)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Filtre chaînes (nom, groupe, tvg-id, url)…")
        filt.addWidget(self.search)

        # Table chaînes
        self.table = QtWidgets.QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Nom", "Groupe", "tvg-id", "Risque", "Raisons", "Statut", "URL"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        vc.addWidget(self.table, 1)

        # ---- EPG UI
        epg_box = QtWidgets.QGroupBox("EPG (XMLTV)")
        epg_layout = QtWidgets.QVBoxLayout(epg_box)

        epg_top = QtWidgets.QHBoxLayout()
        epg_layout.addLayout(epg_top)

        self.epg_url = QtWidgets.QLineEdit(r"C:\Users\ludov\Desktop\IPTV_MASTER\epg")
        self.epg_url.setPlaceholderText("URL EPG (ex: http://localhost:3000/guide.xml ou .xml.gz)")
        self.btn_epg_update = QtWidgets.QPushButton("Mettre à jour EPG")

        self.btn_epg_guide = QtWidgets.QPushButton("Guide…")
        self.btn_epg_guide.setEnabled(False)

        epg_top.addWidget(self.epg_url, 1)
        epg_top.addWidget(self.btn_epg_update)
        epg_top.addWidget(self.btn_epg_guide)

        self.lbl_now = QtWidgets.QLabel("Maintenant: —")
        self.lbl_next = QtWidgets.QLabel("Ensuite: —")
        self.lbl_now.setWordWrap(True)
        self.lbl_next.setWordWrap(True)

        epg_layout.addWidget(self.lbl_now)
        epg_layout.addWidget(self.lbl_next)

        vc.addWidget(epg_box, 0)
        self.tabs.addTab(tab_channels, "Chaînes")

        # ---- Tab 3: VLC Player (création paresseuse)
        tab_player = QtWidgets.QWidget()
        self._player_layout = QtWidgets.QVBoxLayout(tab_player)
        self.player_widget: VlcPlayerPanel | None = None
        self._player_placeholder = QtWidgets.QLabel("Lecteur VLC chargé au premier usage.")
        self._player_placeholder.setAlignment(QtCore.Qt.AlignCenter)
        self._player_layout.addWidget(self._player_placeholder, 1)
        self.tabs.addTab(tab_player, "Lecteur")

        # ---- Tab 4: Salon (Quickload)
        self.salon_tab = SalonTab(self, db=self.db, log=self.logln)
        self.salon_tab.quickload_requested.connect(self.on_salon_quickload)
        self.salon_tab.edit_requested.connect(self.on_salon_open_in_editor)
        self.tabs.addTab(self.salon_tab, "Salon")

        # ---- Tab 5: Configuration (thème/style)
        self._theme_specs = discover_themes()
        self._available_themes = list(self._theme_specs.keys())
        cfg = self._load_user_config()
        initial_theme = cfg.get("theme") if cfg.get("theme") in self._available_themes else (self._available_themes[0] if self._available_themes else "light")
        initial_style = cfg.get("style") if cfg.get("style") else "Fusion"
        self._current_theme = initial_theme
        self._current_style = initial_style
        styles = [s for s in QtWidgets.QStyleFactory.keys() if s.lower() != "windowsvista"]
        if not styles:
            styles = ["Fusion", "Windows"]

        self.settings_tab = SettingsTab(
            self,
            themes=self._available_themes,
            initial_theme=initial_theme,
            styles=styles,
            initial_style=initial_style,
        )
        self.settings_tab.theme_changed.connect(self.on_theme_changed)
        self.settings_tab.style_changed.connect(self.on_style_changed)
        self.tabs.addTab(self.settings_tab, "Configuration")
        # Appliquer la config dès le démarrage
        self.on_style_changed(initial_style)
        self.on_theme_changed(initial_theme)

        # ✅ Refresh Qt-safe (au démarrage)
        QtCore.QTimer.singleShot(0, self.salon_tab.refresh)

        # -------------------------
        # Log repliable (dans le splitter)
        # -------------------------
        log_wrap = QtWidgets.QWidget()
        log_v = QtWidgets.QVBoxLayout(log_wrap)
        log_v.setContentsMargins(0, 0, 0, 0)
        log_v.setSpacing(4)

        self.btn_toggle_log = QtWidgets.QToolButton(text="Log", checkable=True, checked=True)
        self.btn_toggle_log.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.btn_toggle_log.setArrowType(QtCore.Qt.DownArrow)
        log_v.addWidget(self.btn_toggle_log)
        self._log_collapsed_h = self.btn_toggle_log.sizeHint().height() + 8
        log_wrap.setMinimumHeight(self._log_collapsed_h)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        log_v.addWidget(self.log, 1)

        self.vsplit.addWidget(log_wrap)

        self.vsplit.setStretchFactor(0, 1)
        self.vsplit.setStretchFactor(1, 0)
        self.vsplit.setSizes([600, 160])

        self.btn_toggle_log.clicked.connect(self._toggle_log)

        # Signals
        self.btn_load.clicked.connect(self.on_load_file)
        self.btn_load_url.clicked.connect(self.on_load_url)
        self.btn_test.clicked.connect(self.on_test)
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_export.clicked.connect(self.on_export)
        self.btn_export_salon.clicked.connect(self.on_export_salon)
        self.btn_del_dead.clicked.connect(self.on_delete_dead)
        self.btn_del_sel.clicked.connect(self.on_delete_selected)
        self.search.textChanged.connect(self.apply_filter)

        self.btn_refresh_lists.clicked.connect(self.on_refresh_playlists)
        self.btn_load_selected_list.clicked.connect(self.on_load_selected_playlists)
        self.tree.itemSelectionChanged.connect(self.on_tree_selection_changed)
        self.tree.itemClicked.connect(self._tree_click_expand)
        self.list_search.textChanged.connect(self.apply_tree_filter)

        self.playlists_loaded.connect(self._populate_tree)
        self.playlists_error.connect(self._log_error)
        self.import_merged.connect(self._import_merged)

        # VLC + EPG
        self.table.cellDoubleClicked.connect(self.on_channel_double_clicked)
        self.table.itemSelectionChanged.connect(self.on_channel_selected)
        self.btn_epg_update.clicked.connect(self.on_epg_update)
        self.btn_epg_guide.clicked.connect(self.on_epg_guide)

        self.epg_ok.connect(self.on_epg_ok)
        self.epg_fail.connect(self.on_epg_fail)

        self.log_sig.connect(self.log.appendPlainText)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self._playlists_index = None
        self._all_tree_items: list[tuple[QtWidgets.QTreeWidgetItem, str]] = []

    def logln(self, msg: str):
        self.log_sig.emit(msg)

    def _toggle_log(self, checked: bool):
        self.log.setVisible(checked)
        self.btn_toggle_log.setArrowType(QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)

        if checked:
            self.vsplit.setSizes([600, 160])
        else:
            self.vsplit.setSizes([1, self._log_collapsed_h])

    def _on_tab_changed(self, idx: int):
        if idx == 2:  # Lecteur
            self._ensure_player_widget()

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

    def on_style_changed(self, style_name: str):
        app = QtWidgets.QApplication.instance()
        if not app:
            return
        if style_name and style_name in QtWidgets.QStyleFactory.keys():
            app.setStyle(style_name)
            self._current_style = style_name
            self._save_user_config()

    # -------- Salon --------

    def on_salon_quickload(self, playlist_id: int):
        try:
            rows = self.db.get_channels(int(playlist_id))
        except Exception as e:
            self.logln(f"Salon: erreur DB: {e}")
            return

        channels = []
        for r in rows:
            channels.append(Channel(
                extinf=r.get("extinf", ""),
                url=r.get("url", ""),
                name=r.get("name", ""),
                group=r.get("group", ""),
                tvg_id=r.get("tvg_id", ""),
                status="—",
            ))

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

        self.logln(f"Salon: chargé dans le player ({len(channels)} chaînes).")
        self.tabs.setCurrentIndex(2)  # basculer sur Lecteur

    def on_salon_open_in_editor(self, playlist_id: int):
        try:
            rows = self.db.get_channels(int(playlist_id))
        except Exception as e:
            self.logln(f"Salon: erreur DB: {e}")
            return

        channels: list[Channel] = []
        for r in rows:
            channels.append(Channel(
                extinf=r.get("extinf", ""),
                url=r.get("url", ""),
                name=r.get("name", ""),
                group=r.get("group", ""),
                tvg_id=r.get("tvg_id", ""),
                status="—",
            ))

        if not channels:
            self.logln("Salon: playlist vide.")
            return

        self._log_risk_overview(channels)
        # ✅ charge dans l’éditeur (onglet Chaînes)
        self.channels = channels
        self.refresh_table(self.channels)
        self.search.clear()
        self.logln(f"Éditeur: playlist Salon chargée ({len(channels)} chaînes).")

        # optionnel: garder en mémoire quelle playlist on édite
        self._editing_playlist_id = int(playlist_id)

        self.tabs.setCurrentIndex(1)  # onglet "Chaînes"


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
        self.tabs.setCurrentIndex(2)

    # -------- EPG --------

    def on_epg_update(self):
        raw = self.epg_url.text().strip()
        if not raw:
            self.logln("EPG: URL/dossier vide.")
            return

        self.btn_epg_update.setEnabled(False)
        self.btn_epg_guide.setEnabled(False)

        def run():
            try:
                p = Path(raw)

                # If the target is a local npm repo, generate XMLTV locally; otherwise download the remote feed.
                if p.exists() and p.is_dir() and (p / "package.json").exists():
                    self.logln(f"EPG: génération via repo npm: {p}")

                    tvg_ids = [c.tvg_id for c in self.channels if (c.tvg_id or "").strip()]
                    xml = generate_xmltv_for_tvg_ids(
                        repo=p,
                        tvg_ids=tvg_ids,
                        days=1,
                        timeout_s=900,
                        log=self.logln,
                    )
                else:
                    self.logln(f"EPG: téléchargement + import: {raw}")
                    xml = download_xmltv(raw)

                self.db.clear_epg()
                self.db.upsert_epg_programs(iter_programs(xml))
                self.epg_loaded = True
                self.epg_ok.emit()

            except Exception as e:
                self.epg_fail.emit(str(e))

        threading.Thread(target=run, daemon=True).start()

    @QtCore.Slot()
    def on_epg_ok(self):
        self.btn_epg_update.setEnabled(True)
        self.btn_epg_guide.setEnabled(True)
        self.logln("EPG: import OK.")
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
        self.logln(f"EPG: erreur import: {err}")

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
            self.lbl_now.setText("Maintenant: —")
            self.lbl_next.setText("Ensuite: —")
            return

        row = sel[0].row()
        tvg_item = self.table.item(row, 2)
        if not tvg_item:
            return

        tvg_id = (tvg_item.text() or "").strip()
        if not tvg_id:
            self.lbl_now.setText("Maintenant: (pas de tvg-id)")
            self.lbl_next.setText("Ensuite: —")
            return

        if not self.epg_loaded:
            self.lbl_now.setText("Maintenant: (EPG pas chargé)")
            self.lbl_next.setText("Ensuite: —")
            return

        now_ts = int(time.time())
        nowp, nextp = self.db.get_now_next(tvg_id, now_ts)

        def fmt(p):
            if not p:
                return "—"
            st = time.strftime("%H:%M", time.localtime(p["start_ts"]))
            en = time.strftime("%H:%M", time.localtime(p["stop_ts"]))
            title = p.get("title") or ""
            return f"{st}-{en}  {title}"

        self.lbl_now.setText("Maintenant: " + fmt(nowp))
        self.lbl_next.setText("Ensuite: " + fmt(nextp))

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

    def refresh_table(self, data: list[Channel] | None = None):
        data = data if data is not None else self.channels
        self.table.setRowCount(len(data))
        for row, ch in enumerate(data):
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(ch.name))
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
        if not q:
            self.refresh_table()
            return

        filtered = []
        for ch in self.channels:
            hay = f"{ch.name} {ch.group} {ch.tvg_id} {ch.status} {ch.url} {ch.risk_level} {ch.risk_reasons} {ch.risk_score}".lower()
            if q in hay:
                filtered.append(ch)
        self.refresh_table(filtered)

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

    def import_m3u_text(self, text: str, label: str = ""):
        self.channels = parse_m3u(text)
        self._log_risk_overview(self.channels)
        if self.player_widget is not None:
            try:
                self.player_widget.set_channels_from_objects(self.channels)
            except Exception:
                pass
        self.logln(f"Importé: {len(self.channels)} chaînes {('(' + label + ')') if label else ''}")
        self.apply_filter()
        self.tabs.setCurrentIndex(1)

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
        self.btn_load_url.setEnabled(False)
        self.logln(f"Téléchargement: {url}")

        def run():
            try:
                text = requests.get(url, timeout=20).text
                self.import_merged.emit(text, url)
            except Exception as e:
                self.logln(f"Erreur téléchargement: {e}")
            finally:
                QtCore.QTimer.singleShot(0, self, lambda: self.btn_load_url.setEnabled(True))

        threading.Thread(target=run, daemon=True).start()

    def on_test(self):
        if not self.channels:
            self.logln("Aucune playlist chargée.")
            return
        if self._probe_thread:
            self.logln("Test déjà en cours.")
            return

        self.logln("Début test URLs…")
        self.btn_test.setEnabled(False)
        self.btn_stop.setEnabled(True)

        self._probe_thread = QtCore.QThread(self)
        self._probe_worker = ProbeWorker(self.channels, timeout_s=4.0)
        self._probe_worker.moveToThread(self._probe_thread)

        self._probe_thread.started.connect(self._probe_worker.run)
        self._probe_worker.progress.connect(self.on_probe_progress)
        self._probe_worker.finished.connect(self.on_probe_finished)
        self._probe_worker.finished.connect(self._probe_thread.quit)
        self._probe_worker.finished.connect(self._probe_worker.deleteLater)

        self.search.setEnabled(False)
        self.table.setUpdatesEnabled(False)
        self.refresh_table()
        self.table.resizeColumnsToContents()
        self.table.setUpdatesEnabled(True)

        self._probe_thread.start()

    def on_export_salon(self):
        data = self.get_filtered_channels()
        if not data:
            self.logln("Salon: rien à exporter (liste vide/filtre vide).")
            return

        name, ok = QtWidgets.QInputDialog.getText(
            self, "Exporter au Salon", "Nom de la playlist (Salon) :"
        )
        if not ok or not name.strip():
            return
        name = name.strip()

        url = "-"  # optionnel (source)
        pid = self.db.add_playlist(name, url)
        payload = [
            {"name": c.name, "group": c.group, "tvg_id": c.tvg_id, "url": c.url, "extinf": c.extinf}
            for c in data
        ]
        self.db.replace_channels(pid, payload)

        self.logln(f"Salon: export OK -> '{name}' ({len(payload)} chaînes).")
        try:
            self.salon_tab.refresh()
        except Exception:
            pass

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
        self.logln("Test terminé.")
        self.btn_test.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.search.setEnabled(True)

        if self._probe_thread is not None:
            self._probe_thread.quit()
            self._probe_thread.wait(2000)

        self._probe_thread = None
        self._probe_worker = None

    def on_stop(self):
        if self._probe_worker:
            self._probe_worker.stop()
            self.logln("Stop demandé…")

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
        self.logln(f"Supprimé sélection: {before - len(self.channels)}")
        self.apply_filter()

    def on_export(self):
        if not self.channels:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Exporter playlist filtrée", "playlist.filtered.m3u", "M3U (*.m3u)"
        )
        if not path:
            return
        write_m3u(self.channels, Path(path))
        self.logln(f"Exporté: {path}")

    # -------- Playlist Browser --------

    def _tree_click_expand(self, item, column):
        if not item.text(1).strip():
            item.setExpanded(not item.isExpanded())

    def on_tree_selection_changed(self):
        self.btn_load_selected_list.setEnabled(len(self.tree.selectedItems()) > 0)

    def on_refresh_playlists(self):
        self.btn_refresh_lists.setEnabled(False)
        self.btn_load_selected_list.setEnabled(False)
        self.tree.clear()
        self._all_tree_items.clear()
        self.logln("Récupération PLAYLISTS.md…")

        def run():
            try:
                idx = fetch_playlists_index()
                self.playlists_loaded.emit(idx)
            except Exception as e:
                self.playlists_error.emit(str(e))

        threading.Thread(target=run, daemon=True).start()

    @QtCore.Slot(str)
    def _log_error(self, err: str):
        self.logln(f"Erreur: {err}")
        self.btn_refresh_lists.setEnabled(True)

    @QtCore.Slot(dict)
    def _populate_tree(self, idx: dict):
        self._playlists_index = idx
        self.tree.clear()
        self._all_tree_items.clear()

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
        self.btn_refresh_lists.setEnabled(True)
        self.logln("OK: playlists chargées. Déplie Category/Language/Country puis sélectionne → « Charger la sélection ».")

    def apply_tree_filter(self):
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

        def run():
            merged_texts = []
            for url in urls_unique:
                try:
                    t = requests.get(url, timeout=25).text
                    merged_texts.append(t)
                except Exception as e:
                    merged_texts.append("")
                    self.playlists_error.emit(f"KO {url}: {e}")

            # Merge simple: concatène toutes les lignes non vides (en gardant un seul EXTM3U).
            out = ["#EXTM3U"]
            for t in merged_texts:
                for line in t.splitlines():
                    if line.strip() and line.strip() != "#EXTM3U":
                        out.append(line.rstrip())
            final = "\n".join(out) + "\n"

            label = ", ".join(urls_unique[:3]) + ("…" if len(urls_unique) > 3 else "")
            self.import_merged.emit(final, label)

        threading.Thread(target=run, daemon=True).start()

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
            data = {"theme": self._current_theme, "style": self._current_style}
            self.config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass
