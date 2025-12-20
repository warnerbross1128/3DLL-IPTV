from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
import time
from typing import Callable, Optional

import vlc
from PySide6 import QtCore, QtGui, QtWidgets

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
    vlc_opts: list[str] = field(default_factory=list)


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
# =========================
# EPG "vrai guide TV" (grille)
# - Colonne Chaîne + timeline par pas (15/30/60 min)
# - Détails du programme sélectionné (desc + now/next)
# =========================
class EpgGridGuide(QtWidgets.QWidget):
    channel_selected = QtCore.Signal(int)  # channel index (dans self._channels)
    channel_activated = QtCore.Signal(int)  # double-clic -> lecture

    def __init__(
        self,
        parent=None,
        *,
        get_now_next: Optional[Callable[[str, int], tuple[Optional[dict], Optional[dict]]]] = None,
        list_programs: Optional[Callable[[str, int, int, int], list[dict]]] = None,
        log: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(parent)

        self._channels: list[PlayableChannel] = []
        self._visible_idx: list[int] = []
        self._current_idx: Optional[int] = None

        self._get_now_next = get_now_next
        self._list_programs = list_programs
        self._log = log or (lambda _msg: None)

        self.txt_filter = QtWidgets.QLineEdit()
        self.txt_filter.setPlaceholderText('Filtre chaines (nom, groupe, tvg-id)...')

        self.dt_start = QtWidgets.QDateTimeEdit(QtCore.QDateTime.currentDateTime())
        self.dt_start.setCalendarPopup(True)
        self.dt_start.setDisplayFormat('yyyy-MM-dd HH:mm')

        self.hours = QtWidgets.QSpinBox()
        self.hours.setRange(1, 72)
        self.hours.setValue(6)

        self.step = QtWidgets.QComboBox()
        self.step.addItems(['15', '30', '60'])
        self.step.setCurrentText('30')

        self.max_channels = QtWidgets.QSpinBox()
        self.max_channels.setRange(5, 500)
        self.max_channels.setValue(60)
        self.max_channels.setToolTip('Limite le nombre de chaines affichees pour garder le guide reactif.')

        self.btn_refresh = QtWidgets.QPushButton('Rafraichir')
        self.btn_refresh.setSizePolicy(QtWidgets.QSizePolicy.Maximum, QtWidgets.QSizePolicy.Fixed)

        top_row = QtWidgets.QHBoxLayout()
        top_row.addWidget(self.txt_filter, 1)
        top_row.addWidget(self.btn_refresh, 0, QtCore.Qt.AlignRight)

        params_row = QtWidgets.QHBoxLayout()
        params_row.setSpacing(6)
        params_row.addWidget(QtWidgets.QLabel('Debut'))
        params_row.addWidget(self.dt_start)
        params_row.addWidget(QtWidgets.QLabel('Heures'))
        params_row.addWidget(self.hours)
        params_row.addWidget(QtWidgets.QLabel('Pas (min)'))
        params_row.addWidget(self.step)
        params_row.addWidget(QtWidgets.QLabel('Max'))
        params_row.addWidget(self.max_channels)
        params_row.addStretch(1)

        self.tbl = QtWidgets.QTableWidget(0, 0)
        self.tbl.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.tbl.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectItems)
        self.tbl.verticalHeader().setDefaultSectionSize(36)
        self.tbl.horizontalHeader().setMinimumSectionSize(60)
        self.tbl.setWordWrap(False)
        self.tbl.setAlternatingRowColors(True)

        self.lbl_channel = QtWidgets.QLabel('Chaine: -')
        self.lbl_now = QtWidgets.QLabel('Maintenant: -')
        self.lbl_next = QtWidgets.QLabel('Ensuite: -')
        self.lbl_now.setWordWrap(True)
        self.lbl_next.setWordWrap(True)

        details = QtWidgets.QVBoxLayout()
        details.addWidget(self.lbl_channel)
        details.addWidget(self.lbl_now)
        details.addWidget(self.lbl_next)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)
        root.addLayout(top_row)
        root.addLayout(params_row)
        root.addWidget(self.tbl, 2)
        root.addLayout(details, 1)

        self._program_by_cell: dict[tuple[int, int], dict] = {}
        self._row_to_channel_idx: list[int] = []

        self.txt_filter.textChanged.connect(self.refresh)
        self.dt_start.dateTimeChanged.connect(self.refresh)
        self.hours.valueChanged.connect(self.refresh)
        self.step.currentTextChanged.connect(self.refresh)
        self.max_channels.valueChanged.connect(self.refresh)
        self.btn_refresh.clicked.connect(self.refresh)
        self.tbl.cellClicked.connect(self._on_cell_clicked)
        self.tbl.cellDoubleClicked.connect(self._on_cell_double_clicked)

    def set_epg_callbacks(
        self,
        *,
        get_now_next: Optional[Callable[[str, int], tuple[Optional[dict], Optional[dict]]]] = None,
        list_programs: Optional[Callable[[str, int, int, int], list[dict]]] = None,
    ):
        self._get_now_next = get_now_next
        self._list_programs = list_programs
        self.refresh()

    def set_channels(self, channels: list[PlayableChannel]):
        self._channels = channels or []
        self._current_idx = None
        self.refresh()

    def visible_indices(self) -> list[int]:
        return list(self._visible_idx)

    def current_channel_index(self) -> Optional[int]:
        return self._current_idx

    def set_current_channel_index(self, idx: Optional[int]):
        if idx is not None:
            idx = int(idx)
            if idx < 0 or idx >= len(self._channels):
                idx = None
        self._current_idx = idx
        self._update_channel_labels()
        self._select_row_for_current_channel()

    def select_by_url(self, url: str) -> bool:
        url = (url or '').strip()
        if not url:
            return False

        idx = -1
        for i, ch in enumerate(self._channels):
            if (ch.url or '').strip() == url:
                idx = i
                break
        if idx < 0:
            return False

        if idx not in self._visible_idx and (self.txt_filter.text() or '').strip():
            self.txt_filter.blockSignals(True)
            self.txt_filter.setText('')
            self.txt_filter.blockSignals(False)
            self.refresh()

        self.set_current_channel_index(idx)
        return True

    def refresh(self):
        channels = self._channels or []
        q = (self.txt_filter.text() or '').strip().lower()
        max_n = int(self.max_channels.value())

        visible: list[int] = []
        for i, ch in enumerate(channels):
            hay = f'{ch.name} {ch.group} {ch.tvg_id} {ch.url}'.lower()
            if (not q) or (q in hay):
                visible.append(i)
                if len(visible) >= max_n:
                    break
        self._visible_idx = visible
        self._row_to_channel_idx = list(self._visible_idx)

        start_ts = int(self.dt_start.dateTime().toSecsSinceEpoch())
        hours = int(self.hours.value())
        stop_ts = start_ts + hours * 3600

        try:
            step_min = int(self.step.currentText() or '30')
        except Exception:
            step_min = 30
        step_s = max(60, step_min * 60)

        slot_count = max(1, int(math.ceil((stop_ts - start_ts) / float(step_s))))

        self._program_by_cell.clear()
        self.tbl.clear()
        self.tbl.setRowCount(len(self._visible_idx))
        self.tbl.setColumnCount(1 + slot_count)

        labels = ['Chaine']
        for i in range(slot_count):
            ts = start_ts + i * step_s
            labels.append(time.strftime('%H:%M', time.localtime(ts)))
        self.tbl.setHorizontalHeaderLabels(labels)
        self.tbl.horizontalHeader().setStretchLastSection(False)
        self.tbl.horizontalHeader().setDefaultSectionSize(110)
        self.tbl.setColumnWidth(0, 220)

        now_ts = int(time.time())
        pal = self.tbl.palette()
        now_bg = pal.color(QtGui.QPalette.ColorRole.Highlight)
        now_fg = pal.color(QtGui.QPalette.ColorRole.HighlightedText)
        now_brush = QtGui.QBrush(now_bg)
        now_pen = QtGui.QBrush(now_fg)

        def mk_item(text: str) -> QtWidgets.QTableWidgetItem:
            it = QtWidgets.QTableWidgetItem(text or '')
            it.setFlags(it.flags() & ~QtCore.Qt.ItemIsEditable)
            return it

        for row, ch_idx in enumerate(self._visible_idx):
            ch = channels[ch_idx]
            self.tbl.setItem(row, 0, mk_item(ch.name or '(sans nom)'))

            tvg_id = (ch.tvg_id or '').strip()
            if not tvg_id or not self._list_programs:
                continue

            try:
                programs = self._list_programs(tvg_id, start_ts, stop_ts, 400)
            except Exception as e:
                self.tbl.setItem(row, 1, mk_item(f'(Erreur EPG: {type(e).__name__})'))
                continue

            for p in programs or []:
                try:
                    p_start = int(p['start_ts'])
                    p_stop = int(p['stop_ts'])
                except Exception:
                    continue

                a = max(start_ts, p_start)
                b = min(stop_ts, p_stop)
                if b <= a:
                    continue

                start_slot = int((a - start_ts) // step_s)
                end_slot = int(math.ceil((b - start_ts) / float(step_s)))
                start_slot = max(0, min(slot_count - 1, start_slot))
                end_slot = max(start_slot + 1, min(slot_count, end_slot))

                col = 1 + start_slot
                span = end_slot - start_slot

                title = (p.get('title') or '').strip() or '(sans titre)'
                it = mk_item(title)
                it.setToolTip(title)
                if p_start <= now_ts < p_stop:
                    it.setBackground(now_brush)
                    it.setForeground(now_pen)
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)

                self.tbl.setItem(row, col, it)
                if span > 1:
                    self.tbl.setSpan(row, col, 1, span)

                meta = dict(p)
                meta['_channel_idx'] = ch_idx
                self._program_by_cell[(row, col)] = meta

        if self._current_idx is None and self._visible_idx:
            self._current_idx = self._visible_idx[0]

        self._update_channel_labels()
        self._select_row_for_current_channel()

    def _select_row_for_current_channel(self):
        if self._current_idx is None:
            return
        if self._current_idx not in self._row_to_channel_idx:
            return
        row = self._row_to_channel_idx.index(self._current_idx)
        if row < 0 or row >= self.tbl.rowCount():
            return
        self.tbl.blockSignals(True)
        self.tbl.setCurrentCell(row, 0)
        self.tbl.blockSignals(False)

    def _program_for_cell(self, row: int, col: int) -> Optional[dict]:
        if col <= 0:
            return None
        for c in range(col, 0, -1):
            p = self._program_by_cell.get((row, c))
            if p is not None:
                return p
        return None

    def _update_channel_labels(self):
        ch = None
        if self._current_idx is not None and 0 <= self._current_idx < len(self._channels):
            ch = self._channels[self._current_idx]

        if not ch:
            self.lbl_channel.setText('Chaine: -')
            self.lbl_now.setText('Maintenant: -')
            self.lbl_next.setText('Ensuite: -')
            return

        label = ch.name or '(sans nom)'
        if ch.group:
            label = f'{label}   [{ch.group}]'
        if ch.tvg_id:
            label = f'{label}   ({ch.tvg_id})'
        self.lbl_channel.setText('Chaine: ' + label)

        tvg_id = (ch.tvg_id or '').strip()
        if not tvg_id or not self._get_now_next:
            self.lbl_now.setText('Maintenant: -')
            self.lbl_next.setText('Ensuite: -')
            return

        now_ts = int(time.time())
        try:
            nowp, nextp = self._get_now_next(tvg_id, now_ts)
        except Exception as e:
            self.lbl_now.setText(f'Maintenant: (erreur EPG: {type(e).__name__})')
            self.lbl_next.setText('Ensuite: -')
            return

        def fmt(p: Optional[dict]) -> str:
            if not p:
                return '-'
            st = time.strftime('%H:%M', time.localtime(int(p['start_ts'])))
            en = time.strftime('%H:%M', time.localtime(int(p['stop_ts'])))
            title = (p.get('title') or '').strip()
            return f'{st}-{en}  {title}' if title else f'{st}-{en}'

        self.lbl_now.setText('Maintenant: ' + fmt(nowp))
        self.lbl_next.setText('Ensuite: ' + fmt(nextp))

    def _on_cell_clicked(self, row: int, col: int):
        if row < 0 or row >= len(self._row_to_channel_idx):
            return
        ch_idx = self._row_to_channel_idx[row]
        self._current_idx = ch_idx
        self._update_channel_labels()
        self.channel_selected.emit(ch_idx)

        p = self._program_for_cell(row, col)
        # La grille affiche le titre en cellule (et tooltip). Pas de panneau description.

    def _on_cell_double_clicked(self, row: int, _col: int):
        if row < 0 or row >= len(self._row_to_channel_idx):
            return
        ch_idx = self._row_to_channel_idx[row]
        self._current_idx = ch_idx
        self._update_channel_labels()
        self.channel_activated.emit(ch_idx)
class VlcPlayerWidget(QtWidgets.QWidget):
    """Widget VLC réutilisable (PySide6 + python-vlc)"""

    prev_requested = QtCore.Signal()
    next_requested = QtCore.Signal()

    def __init__(self, parent=None, vlc_args=None):
        super().__init__(parent)

        # Surface vidéo
        self.video = QtWidgets.QFrame()
        self.video.setMinimumHeight(240)
        self.video.setAttribute(QtCore.Qt.WA_NativeWindow, True)

        # Contrôles (style Media Player Example)
        self.btn_prev = QtWidgets.QToolButton()
        self.btn_prev.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_MediaSkipBackward))
        self.btn_prev.setToolTip("Chaine precedente (playlist)")

        self.play_button = QtWidgets.QToolButton()
        self.play_button.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_MediaPlay))

        self.stop_button = QtWidgets.QToolButton()
        self.stop_button.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_MediaStop))

        self.btn_next = QtWidgets.QToolButton()
        self.btn_next.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_MediaSkipForward))
        self.btn_next.setToolTip("Chaine suivante (playlist)")

        self.position_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.position_slider.setRange(0, 1000)  # 0..1000 -> 0..1 pour VLC

        self.lbl_time = QtWidgets.QLabel("--:-- / --:--")

        self.mute_button = QtWidgets.QToolButton()
        self.mute_button.setCheckable(True)
        self.mute_button.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_MediaVolume))

        self.volume_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setToolTip("Volume VLC (0-100)")
        self.volume_slider.setMinimumWidth(120)

        self.btn_fullscreen = QtWidgets.QToolButton()
        self.btn_fullscreen.setText("Plein écran")

        self.controls_widget = QtWidgets.QWidget()
        self.controls_widget.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        controls = QtWidgets.QHBoxLayout(self.controls_widget)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)
        controls.addWidget(self.btn_prev)
        controls.addWidget(self.play_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.btn_next)
        controls.addWidget(self.position_slider, 1)
        controls.addWidget(self.lbl_time)
        controls.addWidget(self.mute_button)
        controls.addWidget(self.volume_slider)
        controls.addWidget(self.btn_fullscreen)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.video, 1)
        layout.addWidget(self.controls_widget)

        # VLC
        args = vlc_args or ["--quiet"]
        self.instance = vlc.Instance(*args)
        self.player = self.instance.media_player_new()

        # State
        self._user_scrubbing = False
        self._fullscreen = False
        self._fs_window: Optional[QtWidgets.QWidget] = None
        self._fs_prev_parent: Optional[QtWidgets.QWidget] = None
        self._fs_prev_video_index: int = -1
        self._fs_prev_controls_index: int = -1
        self._controls_hide_timer = QtCore.QTimer(self)
        self._controls_hide_timer.setSingleShot(True)
        self._controls_hide_timer.setInterval(5000)
        self._controls_hide_timer.timeout.connect(self._hide_controls_if_outside)
        self._cursor_poll_timer = QtCore.QTimer(self)
        self._cursor_poll_timer.setInterval(200)
        self._cursor_poll_timer.timeout.connect(self._check_cursor_overlay)

        # Timer refresh UI
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._refresh_ui)

        # Signals
        self.btn_prev.clicked.connect(self.prev_requested.emit)
        self.play_button.clicked.connect(self._toggle_play_pause)
        self.stop_button.clicked.connect(self.stop)
        self.btn_next.clicked.connect(self.next_requested.emit)
        self.mute_button.toggled.connect(self._toggle_mute)
        self.volume_slider.valueChanged.connect(self._on_volume)
        self.position_slider.sliderMoved.connect(self._set_position_from_slider)
        self.position_slider.sliderPressed.connect(self._scrub_start)
        self.position_slider.sliderReleased.connect(self._scrub_end)
        self.btn_fullscreen.clicked.connect(self.toggle_fullscreen)

        # Raccourcis clavier pour plein écran (F / Esc)
        for w in (self, self.video):
            w.setFocusPolicy(QtCore.Qt.StrongFocus)
            w.installEventFilter(self)
            w.setMouseTracking(True)
            w.setAttribute(QtCore.Qt.WA_Hover, True)
        self.controls_widget.setMouseTracking(True)
        self.controls_widget.installEventFilter(self)
        self.controls_widget.setFocusPolicy(QtCore.Qt.NoFocus)

        # Embedding après création native
        QtCore.QTimer.singleShot(0, self._init_embedding)

        # Applique le volume initial dès l'init (sinon VLC reste au volume par défaut tant qu'on ne touche pas au slider)
        self._on_volume(self.volume_slider.value())

    def _init_embedding(self):
        # Windows embedding
        self.player.set_hwnd(int(self.video.winId()))

    # --- API publique ---
    def toggle_fullscreen(self):
        self.set_fullscreen(not self._fullscreen)

    def set_fullscreen(self, enabled: bool):
        enabled = bool(enabled)
        if enabled == self._fullscreen:
            return

        if enabled:
            # détacher la vidéo dans une fenêtre plein écran dédiée
            parent_layout = self.layout()
            if parent_layout:
                self._fs_prev_video_index = parent_layout.indexOf(self.video)
                parent_layout.removeWidget(self.video)
                self._fs_prev_controls_index = parent_layout.indexOf(self.controls_widget)
                parent_layout.removeWidget(self.controls_widget)
            self._fs_prev_parent = self.video.parentWidget()
            self.video.setParent(None)
            self.controls_widget.setParent(None)

            self._fs_window = QtWidgets.QWidget(None, QtCore.Qt.Window | QtCore.Qt.FramelessWindowHint)
            self._fs_window.setAttribute(QtCore.Qt.WA_DeleteOnClose, False)
            self._fs_window.installEventFilter(self)
            self._fs_window.setMouseTracking(True)

            fs_layout = QtWidgets.QVBoxLayout(self._fs_window)
            fs_layout.setContentsMargins(0, 0, 0, 0)
            fs_layout.setSpacing(0)
            fs_layout.addWidget(self.video, 1)
            fs_layout.addWidget(self.controls_widget)

            self._fs_window.showFullScreen()
            self._fs_window.raise_()
            self._fullscreen = True
            self.controls_widget.setVisible(False)
            self._controls_hide_timer.stop()
            self._controls_hide_timer.start()
            self.controls_widget.raise_()
            self._cursor_poll_timer.start()
            QtCore.QTimer.singleShot(0, self._init_embedding)
        else:
            # Replacer la vidéo dans le layout d'origine et fermer la fenêtre FS
            self._fullscreen = False  # éviter re-entrées
            fs_window = self._fs_window
            self._fs_window = None
            if fs_window:
                try:
                    fs_window.removeEventFilter(self)
                except Exception:
                    pass
                try:
                    layout_fs = fs_window.layout()
                    if layout_fs:
                        layout_fs.removeWidget(self.video)
                except Exception:
                    pass
            try:
                self.video.setParent(self)
                self.controls_widget.setParent(self)
            except Exception:
                pass
            parent_layout = self.layout()
            if parent_layout is not None:
                try:
                    # Assure une pile verticale: vidéo puis contrôles en bas.
                    while parent_layout.count():
                        item = parent_layout.takeAt(0)
                        w = item.widget()
                        if w:
                            w.setParent(None)
                    parent_layout.addWidget(self.video, 1)
                    parent_layout.addWidget(self.controls_widget, 0)
                except Exception:
                    pass
            if fs_window:
                try:
                    fs_window.hide()
                    fs_window.close()
                except Exception:
                    pass
            self.controls_widget.setVisible(True)
            self._controls_hide_timer.stop()
            self._cursor_poll_timer.stop()
            self._fs_prev_video_index = -1
            self._fs_prev_controls_index = -1
            QtCore.QTimer.singleShot(0, self._init_embedding)
        self.btn_fullscreen.setText("Quitter plein écran" if self._fullscreen else "Plein écran")

    def set_url(self, url: str, vlc_opts: Optional[list[str]] = None):
        media = self.instance.media_new(url)
        for opt in vlc_opts or []:
            opt = (opt or "").strip()
            if not opt:
                continue
            if not opt.startswith(":"):
                opt = ":" + opt
            media.add_option(opt)
        self.player.set_media(media)

    def play_url(self, url: str, vlc_opts: Optional[list[str]] = None):
        self.set_url(url, vlc_opts)
        self.play()

    def play(self):
        self.player.play()
        self.timer.start()
        self._set_play_icon(True)

    def pause(self):
        self.player.pause()
        self._set_play_icon(False)

    def stop(self):
        try:
            self.player.stop()
        finally:
            self.timer.stop()
            self.position_slider.setValue(0)
            self.lbl_time.setText("--:-- / --:--")
            self._set_play_icon(False)

    def shutdown(self):
        """À appeler à la fermeture de l'app."""
        self.set_fullscreen(False)
        self.stop()

    def set_zap_enabled(self, enabled: bool):
        enabled = bool(enabled)
        self.btn_prev.setEnabled(enabled)
        self.btn_next.setEnabled(enabled)

    # --- internes ---
    def _on_volume(self, v: int):
        self.player.audio_set_volume(v)

    def _toggle_mute(self, muted: bool):
        self.player.audio_set_mute(bool(muted))
        icon = QtWidgets.QStyle.SP_MediaVolumeMuted if muted else QtWidgets.QStyle.SP_MediaVolume
        self.mute_button.setIcon(self.style().standardIcon(icon))

    def _scrub_start(self):
        self._user_scrubbing = True

    def _scrub_end(self):
        self.player.set_position(self.position_slider.value() / 1000.0)
        self._user_scrubbing = False

    def _set_position_from_slider(self, value: int):
        try:
            self.player.set_position(value / 1000.0)
        except Exception:
            pass

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() == QtCore.QEvent.KeyPress:
            key = event.key()
            if key == QtCore.Qt.Key_F:
                self.toggle_fullscreen()
                return True
            if key == QtCore.Qt.Key_C and self._fullscreen:
                if self.controls_widget.isVisible():
                    self._controls_hide_timer.stop()
                    self.controls_widget.setVisible(False)
                else:
                    self._show_controls_overlay(force=True)
                return True
            if key == QtCore.Qt.Key_Escape and self._fullscreen:
                self.set_fullscreen(False)
                return True
        if obj is self.video:
            if event.type() == QtCore.QEvent.MouseButtonPress:
                self._toggle_play_pause()
                return True
            if event.type() in (QtCore.QEvent.MouseMove, QtCore.QEvent.HoverMove) and self._fullscreen:
                self._maybe_show_controls(obj, event)
        if self._fs_window and obj is self._fs_window:
            if event.type() == QtCore.QEvent.KeyPress:
                key = event.key()
                if key == QtCore.Qt.Key_Escape:
                    self.set_fullscreen(False)
                    return True
            if event.type() in (QtCore.QEvent.MouseMove, QtCore.QEvent.HoverMove):
                self._maybe_show_controls(obj, event)
        return super().eventFilter(obj, event)

    def _toggle_play_pause(self):
        try:
            is_playing = bool(self.player.is_playing())
        except Exception:
            is_playing = False
        if is_playing:
            self.pause()
        else:
            self.play()

    def _show_controls_overlay(self, force: bool = False):
        if not self._fullscreen or not self._fs_window:
            return
        self.controls_widget.setVisible(True)
        self.controls_widget.raise_()
        if force:
            self._controls_hide_timer.start()

    def _maybe_show_controls(self, obj: QtCore.QObject, event: QtCore.QEvent):
        if not self._fullscreen or not self._fs_window:
            return
        try:
            local_pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        except Exception:
            return
        try:
            global_pos = obj.mapToGlobal(local_pos)
            win_pos = self._fs_window.mapFromGlobal(global_pos)
        except Exception:
            return
        win_h = max(1, self._fs_window.height())
        threshold = max(80, int(win_h * 0.15))
        near_bottom = win_pos.y() >= win_h - threshold
        if near_bottom or self.controls_widget.underMouse():
            self._show_controls_overlay(force=True)
        else:
            # Si on remonte la souris, on lance le timer pour cacher
            if self.controls_widget.isVisible():
                self._controls_hide_timer.start()

    def _hide_controls_if_outside(self):
        if not self._fullscreen or not self._fs_window:
            return
        cursor_pos = QtGui.QCursor.pos()
        try:
            local_controls = self.controls_widget.mapFromGlobal(cursor_pos)
            local_win = self._fs_window.mapFromGlobal(cursor_pos)
        except Exception:
            self.controls_widget.setVisible(False)
            return
        if self.controls_widget.rect().contains(local_controls):
            self._controls_hide_timer.start()
            return
        win_h = max(1, self._fs_window.height())
        threshold = max(80, int(win_h * 0.15))
        if local_win.y() >= win_h - threshold:
            self._controls_hide_timer.start()
            return
        self.controls_widget.setVisible(False)

    def _check_cursor_overlay(self):
        if not self._fullscreen or not self._fs_window:
            return
        cursor_pos = QtGui.QCursor.pos()
        try:
            local_win = self._fs_window.mapFromGlobal(cursor_pos)
        except Exception:
            return
        win_h = max(1, self._fs_window.height())
        threshold = max(80, int(win_h * 0.15))
        near_bottom = local_win.y() >= win_h - threshold
        if near_bottom:
            self._show_controls_overlay(force=True)

    def _set_play_icon(self, playing: bool):
        icon = QtWidgets.QStyle.SP_MediaPause if playing else QtWidgets.QStyle.SP_MediaPlay
        self.play_button.setIcon(self.style().standardIcon(icon))

    def _refresh_ui(self):
        try:
            is_playing = bool(self.player.is_playing())
            self._set_play_icon(is_playing)
        except Exception:
            pass
        try:
            muted = bool(self.player.audio_get_mute())
            if self.mute_button.isChecked() != muted:
                self.mute_button.blockSignals(True)
                self.mute_button.setChecked(muted)
                self.mute_button.blockSignals(False)
            icon = QtWidgets.QStyle.SP_MediaVolumeMuted if muted else QtWidgets.QStyle.SP_MediaVolume
            self.mute_button.setIcon(self.style().standardIcon(icon))
        except Exception:
            pass

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
                self.position_slider.setValue(int(pos * 1000))


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
        # Colonne gauche: vrai guide TV (grille EPG)
        # -------------------------
        self.epg_grid = EpgGridGuide(
            get_now_next=self._get_now_next,
            list_programs=self._list_programs,
            log=self._log,
        )

        # -------------------------
        # VLC player à droite
        # -------------------------
        self.player = VlcPlayerWidget(vlc_args=vlc_args)

        self._splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self._splitter.addWidget(self.epg_grid)
        self._splitter.addWidget(self.player)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([740, 900])
        self._last_split_sizes = [740, 900]

        # Toggle EPG (masquer/afficher la colonne gauche)
        self.btn_toggle_epg = QtWidgets.QPushButton("Masquer EPG")
        self.btn_toggle_epg.setCheckable(True)
        self.btn_toggle_epg.setChecked(True)
        self.btn_toggle_epg.clicked.connect(self._on_toggle_epg)

        top_controls = QtWidgets.QHBoxLayout()
        top_controls.addWidget(self.btn_toggle_epg, 0, QtCore.Qt.AlignLeft)
        top_controls.addStretch(1)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addLayout(top_controls)
        root.addWidget(self._splitter, 1)

        # State guide
        self._guide_rows: list[dict] = []
        self._epg_visible = True

        # Signals
        self.epg_grid.channel_selected.connect(self._on_channel_selected_from_grid)
        self.epg_grid.channel_activated.connect(self._on_channel_activated_from_grid)

        # Zap (previous/next) depuis les contr“les VLC
        self.player.prev_requested.connect(self.zap_previous)
        self.player.next_requested.connect(self.zap_next)
        self.player.set_zap_enabled(False)

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
        self.epg_grid.set_epg_callbacks(get_now_next=get_now_next, list_programs=list_programs)

    # -------------------------
    # API: playlist
    # -------------------------
    def set_channels(self, channels: list[PlayableChannel]):
        self._channels = channels or []
        self.epg_grid.set_channels(self._channels)
        self.player.set_zap_enabled(bool(self.epg_grid.visible_indices()))

    def set_channels_from_objects(self, channels: list[object]):
        """
        Helper: accepte la liste Channel (iptv_desktop.py) si elle a:
          .name .group .tvg_id .url
        """
        out: list[PlayableChannel] = []
        for c in channels or []:
            raw_opts = getattr(c, "vlc_opts", None)
            if isinstance(raw_opts, (list, tuple)):
                vlc_opts = [str(x) for x in raw_opts if str(x).strip()]
            elif raw_opts:
                vlc_opts = [str(raw_opts).strip()]
            else:
                vlc_opts = []
            out.append(
                PlayableChannel(
                    name=str(getattr(c, "name", "") or ""),
                    group=str(getattr(c, "group", "") or ""),
                    tvg_id=str(getattr(c, "tvg_id", "") or ""),
                    url=str(getattr(c, "url", "") or ""),
                    vlc_opts=vlc_opts,
                )
            )
        self.set_channels(out)

    def current_channel(self) -> Optional[PlayableChannel]:
        idx = self.epg_grid.current_channel_index()
        if idx is None:
            return None
        if idx < 0 or idx >= len(self._channels):
            return None
        return self._channels[idx]

    # -------------------------
    # VLC passthrough
    # -------------------------
    def play_url(self, url: str):
        url = (url or "").strip()
        if not url:
            return

        vlc_opts: list[str] = []
        for ch in self._channels or []:
            if (ch.url or "").strip() == url:
                vlc_opts = list(getattr(ch, "vlc_opts", []) or [])
                break
        try:
            self.epg_grid.select_by_url(url)
        except Exception:
            pass
        self.player.play_url(url, vlc_opts=vlc_opts)

    def shutdown(self):
        try:
            self.player.set_fullscreen(False)
        except Exception:
            pass
        self.player.shutdown()

    # -------------------------
    # Internals
    # -------------------------
    def _play_current_channel(self):
        ch = self.current_channel()
        if not ch:
            return
        url = (ch.url or "").strip()
        if not url:
            return
        self._log(f"Lecture: {url}")
        self.player.play_url(url, vlc_opts=list(getattr(ch, "vlc_opts", []) or []))

    def _on_channel_selected_from_grid(self, _idx: int):
        # Rien a lancer automatiquement: la grille gere les details.
        self.player.set_zap_enabled(bool(self.epg_grid.visible_indices()))

    def _on_channel_activated_from_grid(self, _idx: int):
        self._play_current_channel()

    def _on_toggle_epg(self, checked: bool):
        show = bool(checked)
        self._epg_visible = show
        self.epg_grid.setVisible(show)
        if show:
            self.btn_toggle_epg.setText("Masquer EPG")
            if self._last_split_sizes:
                try:
                    self._splitter.setSizes(self._last_split_sizes)
                except Exception:
                    pass
        else:
            self.btn_toggle_epg.setText("Afficher EPG")
            try:
                self._last_split_sizes = self._splitter.sizes()
            except Exception:
                self._last_split_sizes = [740, 900]
            self._splitter.setSizes([0, 1])

    @QtCore.Slot()
    def zap_next(self):
        self._zap(+1)

    @QtCore.Slot()
    def zap_previous(self):
        self._zap(-1)

    def _zap(self, delta: int):
        visible = self.epg_grid.visible_indices()
        n = len(visible)
        if n <= 0:
            return

        cur = self.epg_grid.current_channel_index()
        if cur is None or cur not in visible:
            self.epg_grid.set_current_channel_index(visible[0])
            self._play_current_channel()
            return

        row = visible.index(cur)
        row = (row + int(delta)) % n
        self.epg_grid.set_current_channel_index(visible[row])
        self._play_current_channel()
