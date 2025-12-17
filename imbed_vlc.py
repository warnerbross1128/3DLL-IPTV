from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import time
from typing import Callable, Optional

import vlc
from PySide6 import QtCore, QtWidgets

# Widgets Qt pour embarquer VLC et afficher playlist/EPG dans le lecteur intégré.


# -------------------------
# Modèle léger pour la playlist du lecteur
# -------------------------
@dataclass
class PlayableChannel:
    name: str
    group: str
    tvg_id: str
    url: str


# =========================
# Collapsible Section (menu déroulant)
# =========================
class CollapsibleBox(QtWidgets.QWidget):
    """
    Un groupe vraiment repliable:
      - header cliquable avec flèche
      - contenu visible/caché (ne prend plus de place quand replié)
      - utilisé pour structurer playlist / now-next / guide
    """

    def __init__(self, title: str, parent=None, *, checked: bool = True):
        super().__init__(parent)

        self.toggle = QtWidgets.QToolButton(text=title, checkable=True, checked=checked)
        self.toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)
        self.toggle.clicked.connect(self._on_toggle)

        self.content = QtWidgets.QWidget()
        self.content.setVisible(checked)

        self._content_layout = QtWidgets.QVBoxLayout(self.content)
        self._content_layout.setContentsMargins(0, 6, 0, 6)
        self._content_layout.setSpacing(6)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)
        root.addWidget(self.toggle)
        root.addWidget(self.content)

    def _on_toggle(self, checked: bool):
        self.content.setVisible(checked)
        self.toggle.setArrowType(QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)

    def addWidget(self, w: QtWidgets.QWidget, stretch: int = 0):
        self._content_layout.addWidget(w, stretch)

    def addLayout(self, l: QtWidgets.QLayout, stretch: int = 0):
        self._content_layout.addLayout(l, stretch)

    def addStretch(self, s: int = 1):
        self._content_layout.addStretch(s)


# =========================
# VLC core widget (inchangé / compatible)
# =========================
class VlcPlayerWidget(QtWidgets.QWidget):
    """Widget VLC réutilisable (PySide6 + python-vlc)"""

    def __init__(self, parent=None, vlc_args=None):
        super().__init__(parent)

        # Surface vidéo
        self.video = QtWidgets.QFrame()
        self.video.setMinimumHeight(240)
        self.video.setAttribute(QtCore.Qt.WA_NativeWindow, True)

        # Contrôles
        self.btn_play = QtWidgets.QPushButton("Play")
        self.btn_pause = QtWidgets.QPushButton("Pause")
        self.btn_stop = QtWidgets.QPushButton("Stop")

        self.slider_pos = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider_pos.setRange(0, 1000)  # 0..1000 -> 0..1 pour VLC

        self.lbl_time = QtWidgets.QLabel("--:-- / --:--")

        self.slider_vol = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(80)

        # Layout
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.btn_play)
        row.addWidget(self.btn_pause)
        row.addWidget(self.btn_stop)
        row.addSpacing(10)
        row.addWidget(self.lbl_time)
        row.addStretch(1)
        row.addWidget(QtWidgets.QLabel("Vol"))
        row.addWidget(self.slider_vol)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.video, 1)
        layout.addWidget(self.slider_pos)
        layout.addLayout(row)

        # VLC
        args = vlc_args or ["--quiet"]
        self.instance = vlc.Instance(*args)
        self.player = self.instance.media_player_new()

        # State
        self._user_scrubbing = False

        # Timer refresh UI
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._refresh_ui)

        # Signals
        self.btn_play.clicked.connect(self.play)
        self.btn_pause.clicked.connect(self.pause)
        self.btn_stop.clicked.connect(self.stop)

        self.slider_vol.valueChanged.connect(self._on_volume)
        self.slider_pos.sliderPressed.connect(self._scrub_start)
        self.slider_pos.sliderReleased.connect(self._scrub_end)

        # Embedding après création native
        QtCore.QTimer.singleShot(0, self._init_embedding)

    def _init_embedding(self):
        # Windows embedding
        self.player.set_hwnd(int(self.video.winId()))

    # --- API publique ---
    def set_url(self, url: str):
        media = self.instance.media_new(url)
        self.player.set_media(media)

    def play_url(self, url: str):
        self.set_url(url)
        self.play()

    def play(self):
        self.player.play()
        self.timer.start()

    def pause(self):
        self.player.pause()

    def stop(self):
        try:
            self.player.stop()
        finally:
            self.timer.stop()
            self.slider_pos.setValue(0)
            self.lbl_time.setText("--:-- / --:--")

    def shutdown(self):
        """À appeler à la fermeture de l'app."""
        self.stop()

    # --- internes ---
    def _on_volume(self, v: int):
        self.player.audio_set_volume(v)

    def _scrub_start(self):
        self._user_scrubbing = True

    def _scrub_end(self):
        self.player.set_position(self.slider_pos.value() / 1000.0)
        self._user_scrubbing = False

    def _refresh_ui(self):
        length = self.player.get_length()
        t = self.player.get_time()

        def fmt(ms: int) -> str:
            if ms < 0:
                return "--:--"
            s = ms // 1000
            m = s // 60
            s = s % 60
            h = m // 60
            m = m % 60
            return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

        if length > 0:
            self.lbl_time.setText(f"{fmt(t)} / {fmt(length)}")

        if not self._user_scrubbing:
            pos = self.player.get_position()
            if pos >= 0:
                self.slider_pos.setValue(int(pos * 1000))


# =========================
# Lecteur complet (UI fix)
# - Colonne gauche scrollable
# - Sections déroulantes
# - Aucun log interne (utilise callback log=MainWindow.logln)
# =========================
class VlcPlayerPanel(QtWidgets.QWidget):
    """
    Panneau lecteur:
      - playlist (filtre + liste) en section repliable
      - now/next en section repliable
      - guide EPG (date + plage + table + desc) en section repliable
      - vidéo VLC à droite
      - log() = callback vers le main (pas de log widget ici)
    """

    def __init__(
        self,
        parent=None,
        vlc_args=None,
        get_now_next: Optional[Callable[[str, int], tuple[Optional[dict], Optional[dict]]]] = None,
        list_programs: Optional[Callable[[str, int, int, int], list[dict]]] = None,
        log: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(parent)

        self._channels: list[PlayableChannel] = []
        self._filtered_idx: list[int] = []
        self._get_now_next = get_now_next
        self._list_programs = list_programs
        self._log = log or (lambda _msg: None)

        # -------------------------
        # Widgets playlist
        # -------------------------
        self.txt_filter = QtWidgets.QLineEdit()
        self.txt_filter.setPlaceholderText("Filtre playlist (nom, groupe, tvg-id)…")

        self.list_channels = QtWidgets.QListWidget()
        self.list_channels.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)

        # -------------------------
        # Widgets Now/Next
        # -------------------------
        self.lbl_now = QtWidgets.QLabel("Maintenant: —")
        self.lbl_next = QtWidgets.QLabel("Ensuite: —")
        self.lbl_now.setWordWrap(True)
        self.lbl_next.setWordWrap(True)

        # -------------------------
        # Widgets guide EPG
        # -------------------------
        guide_top = QtWidgets.QHBoxLayout()
        self.date = QtWidgets.QDateEdit(QtCore.QDate.currentDate())
        self.date.setCalendarPopup(True)

        self.hours = QtWidgets.QSpinBox()
        self.hours.setRange(1, 72)
        self.hours.setValue(24)

        self.btn_refresh_guide = QtWidgets.QPushButton("Rafraîchir guide")
        guide_top.addWidget(self.date)
        guide_top.addWidget(QtWidgets.QLabel("Heures"))
        guide_top.addWidget(self.hours)
        guide_top.addStretch(1)
        guide_top.addWidget(self.btn_refresh_guide)

        self.tbl_guide = QtWidgets.QTableWidget(0, 3)
        self.tbl_guide.setHorizontalHeaderLabels(["Début", "Fin", "Titre"])
        self.tbl_guide.horizontalHeader().setStretchLastSection(True)
        self.tbl_guide.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_guide.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)

        self.txt_desc = QtWidgets.QPlainTextEdit()
        self.txt_desc.setReadOnly(True)
        self.txt_desc.setMaximumBlockCount(3000)
        self.txt_desc.setPlaceholderText("Description…")

        # -------------------------
        # Colonne gauche scrollable + sections
        # -------------------------
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        left_container = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left_container)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(10)
        scroll.setWidget(left_container)

        # Section: Playlist
        box_playlist = CollapsibleBox("Playlist", checked=True)
        box_playlist.addWidget(self.txt_filter)
        box_playlist.addWidget(self.list_channels, 1)
        left_layout.addWidget(box_playlist)

        # Section: Now/Next
        box_now = CollapsibleBox("EPG — Maintenant / Ensuite", checked=True)
        box_now.addWidget(self.lbl_now)
        box_now.addWidget(self.lbl_next)
        left_layout.addWidget(box_now)

        # Section: Guide EPG
        box_guide = CollapsibleBox("Guide EPG", checked=True)
        box_guide.addLayout(guide_top)
        box_guide.addWidget(self.tbl_guide, 1)
        box_guide.addWidget(self.txt_desc, 0)
        left_layout.addWidget(box_guide)

        left_layout.addStretch(1)

        # -------------------------
        # VLC player à droite
        # -------------------------
        self.player = VlcPlayerWidget(vlc_args=vlc_args)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(scroll)
        splitter.addWidget(self.player)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([460, 900])

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(splitter, 1)

        # State guide
        self._guide_rows: list[dict] = []

        # Signals
        self.txt_filter.textChanged.connect(self._apply_filter)
        self.list_channels.itemSelectionChanged.connect(self._on_channel_selected)
        self.list_channels.itemDoubleClicked.connect(self._on_channel_double_clicked)

        self.btn_refresh_guide.clicked.connect(self.refresh_guide)
        self.tbl_guide.itemSelectionChanged.connect(self._on_guide_selected)

    # -------------------------
    # API: wiring callbacks
    # -------------------------
    def set_epg_callbacks(
        self,
        get_now_next: Optional[Callable[[str, int], tuple[Optional[dict], Optional[dict]]]] = None,
        list_programs: Optional[Callable[[str, int, int, int], list[dict]]] = None,
    ):
        """Brancher les callbacks EPG (DB) puis rafraîchir now/next + guide."""
        self._get_now_next = get_now_next
        self._list_programs = list_programs
        self.refresh_now_next()
        self.refresh_guide()

    # -------------------------
    # API: playlist
    # -------------------------
    def set_channels(self, channels: list[PlayableChannel]):
        self._channels = channels or []
        self._apply_filter()

    def set_channels_from_objects(self, channels: list[object]):
        """
        Helper: accepte la liste Channel (iptv_desktop.py) si elle a:
          .name .group .tvg_id .url
        """
        out: list[PlayableChannel] = []
        for c in channels or []:
            out.append(
                PlayableChannel(
                    name=str(getattr(c, "name", "") or ""),
                    group=str(getattr(c, "group", "") or ""),
                    tvg_id=str(getattr(c, "tvg_id", "") or ""),
                    url=str(getattr(c, "url", "") or ""),
                )
            )
        self.set_channels(out)

    def current_channel(self) -> Optional[PlayableChannel]:
        row = self.list_channels.currentRow()
        if row < 0 or row >= len(self._filtered_idx):
            return None
        idx = self._filtered_idx[row]
        if idx < 0 or idx >= len(self._channels):
            return None
        return self._channels[idx]

    # -------------------------
    # VLC passthrough
    # -------------------------
    def play_url(self, url: str):
        self.player.play_url(url)

    def shutdown(self):
        self.player.shutdown()

    # -------------------------
    # Internals: playlist filter/render
    # -------------------------
    def _apply_filter(self):
        q = (self.txt_filter.text() or "").strip().lower()

        self.list_channels.blockSignals(True)
        self.list_channels.clear()
        self._filtered_idx.clear()

        for i, ch in enumerate(self._channels):
            hay = f"{ch.name} {ch.group} {ch.tvg_id} {ch.url}".lower()
            if (not q) or (q in hay):
                self._filtered_idx.append(i)

                label = ch.name or "(sans nom)"
                if ch.group:
                    label = f"{label}   [{ch.group}]"
                if ch.tvg_id:
                    label = f"{label}   ({ch.tvg_id})"

                self.list_channels.addItem(label)

        self.list_channels.blockSignals(False)

        if self.list_channels.count() and self.list_channels.currentRow() < 0:
            self.list_channels.setCurrentRow(0)

        self.refresh_now_next()
        self.refresh_guide()

    def _on_channel_selected(self):
        self.refresh_now_next()
        self.refresh_guide()

    def _on_channel_double_clicked(self, _item):
        ch = self.current_channel()
        if not ch:
            return
        if not (ch.url or "").strip():
            return
        self._log(f"Lecture: {ch.url}")
        self.play_url(ch.url)

    # -------------------------
    # Now/Next + Guide
    # -------------------------
    def refresh_now_next(self):
        ch = self.current_channel()
        if not ch or not (ch.tvg_id or "").strip():
            self.lbl_now.setText("Maintenant: —")
            self.lbl_next.setText("Ensuite: —")
            return

        if not self._get_now_next:
            self.lbl_now.setText("Maintenant: (EPG non branché)")
            self.lbl_next.setText("Ensuite: —")
            return

        now_ts = int(time.time())
        try:
            nowp, nextp = self._get_now_next(ch.tvg_id.strip(), now_ts)
        except Exception as e:
            self.lbl_now.setText(f"Maintenant: (erreur EPG: {type(e).__name__})")
            self.lbl_next.setText("Ensuite: —")
            return

        def fmt(p: Optional[dict]) -> str:
            if not p:
                return "—"
            st = time.strftime("%H:%M", time.localtime(int(p["start_ts"])))
            en = time.strftime("%H:%M", time.localtime(int(p["stop_ts"])))
            title = (p.get("title") or "").strip()
            return f"{st}-{en}  {title}" if title else f"{st}-{en}"

        self.lbl_now.setText("Maintenant: " + fmt(nowp))
        self.lbl_next.setText("Ensuite: " + fmt(nextp))

    def refresh_guide(self):
        ch = self.current_channel()
        if not ch or not (ch.tvg_id or "").strip():
            self._set_guide_rows([])
            self.txt_desc.setPlainText("(Sélectionne une chaîne avec tvg-id.)")
            return

        if not self._list_programs:
            self._set_guide_rows([])
            self.txt_desc.setPlainText("(EPG non branché.)")
            return

        dt = self.date.date()
        start_ts = int(time.time())
        hours = int(self.hours.value())
        stop_ts = start_ts + hours * 3600


        try:
            rows = self._list_programs(ch.tvg_id.strip(), start_ts, stop_ts, 2000)
        except Exception as e:
            self._set_guide_rows([])
            self.txt_desc.setPlainText(f"(Erreur EPG: {type(e).__name__}: {e})")
            return

        self._set_guide_rows(rows)
        if not rows:
            self.txt_desc.setPlainText("(Aucun programme dans cette plage.)")
        else:
            self.txt_desc.setPlainText("Sélectionne une émission pour voir la description.")

    def _set_guide_rows(self, rows: list[dict]):
        self._guide_rows = rows or []
        self.tbl_guide.setRowCount(len(self._guide_rows))

        def fmt_ts(ts: int) -> str:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))

        for i, p in enumerate(self._guide_rows):
            self.tbl_guide.setItem(i, 0, QtWidgets.QTableWidgetItem(fmt_ts(p["start_ts"])))
            self.tbl_guide.setItem(i, 1, QtWidgets.QTableWidgetItem(fmt_ts(p["stop_ts"])))
            self.tbl_guide.setItem(i, 2, QtWidgets.QTableWidgetItem((p.get("title") or "").strip()))

        self.tbl_guide.resizeColumnsToContents()

    def _on_guide_selected(self):
        sel = self.tbl_guide.selectionModel().selectedRows()
        if not sel:
            return
        r = sel[0].row()
        if r < 0 or r >= len(self._guide_rows):
            return

        p = self._guide_rows[r]
        st = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(p["start_ts"])))
        en = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(p["stop_ts"])))
        title = (p.get("title") or "").strip()
        desc = (p.get("desc") or "").strip()

        txt = f"{st} → {en}\n{title}\n\n{desc}" if desc else f"{st} → {en}\n{title}"
        self.txt_desc.setPlainText(txt)
