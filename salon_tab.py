# salon_tab.py
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

# Onglet "Salon": gestion locale des playlists sauvegardées (DB), avec chargement rapide ou ouverture en éditeur.

class SalonTab(QtWidgets.QWidget):
    """
    Salon = gestion des playlists enregistrées en DB.

    - Charger dans le player
    - Ouvrir dans l’éditeur (onglet Chaînes)
    - Supprimer
    """

    quickload_requested = QtCore.Signal(int)   # playlist_id -> player
    edit_requested = QtCore.Signal(int)        # playlist_id -> éditeur Chaînes

    def __init__(self, parent=None, db=None, log=None):
        super().__init__(parent)
        self.db = db
        self.log = log or (lambda s: None)

        layout = QtWidgets.QVBoxLayout(self)

        # ======================
        # Top bar
        # ======================
        hb = QtWidgets.QHBoxLayout()
        layout.addLayout(hb)

        self.btn_refresh = QtWidgets.QPushButton("Rafraîchir")
        self.btn_load = QtWidgets.QPushButton("Charger dans le player")
        self.btn_edit_open = QtWidgets.QPushButton("Ouvrir dans l’éditeur")
        self.btn_delete = QtWidgets.QPushButton("Supprimer")

        self.btn_load.setEnabled(False)
        self.btn_edit_open.setEnabled(False)
        self.btn_delete.setEnabled(False)

        self.txt_search = QtWidgets.QLineEdit()
        self.txt_search.setPlaceholderText("Rechercher (nom, url)…")

        hb.addWidget(self.btn_refresh)
        hb.addWidget(self.btn_load)
        hb.addWidget(self.btn_edit_open)
        hb.addWidget(self.btn_delete)
        hb.addWidget(self.txt_search, 1)

        # ======================
        # Table playlists
        # ======================
        self.tbl = QtWidgets.QTableWidget(0, 3)
        self.tbl.setHorizontalHeaderLabels(["#", "Nom", "Source"])
        self.tbl.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setSortingEnabled(True)
        self.tbl.verticalHeader().setVisible(False)
        layout.addWidget(self.tbl, 1)

        # ======================
        # Events
        # ======================
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_load.clicked.connect(self._load_selected)
        self.btn_edit_open.clicked.connect(self._open_selected_in_editor)
        self.btn_delete.clicked.connect(self._delete_selected)

        self.tbl.itemSelectionChanged.connect(self._sel_changed)
        self.tbl.itemDoubleClicked.connect(lambda *_: self._load_selected())
        self.txt_search.textChanged.connect(self._apply_filter)

        # menu clic droit
        self.tbl.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tbl.customContextMenuRequested.connect(self._context_menu)

        self._rows = []          # playlists DB
        self._visible_rows = []  # index visibles -> _rows

    # ======================
    # Data
    # ======================
    def refresh(self):
        if not self.db:
            self.log("Salon: DB non initialisée.")
            return

        self._rows = self.db.list_playlists()  # PlaylistRec(id, name, url)
        self._apply_filter()
        self.log(f"Salon: {len(self._rows)} playlists en DB.")

    def _apply_filter(self):
        q = (self.txt_search.text() or "").strip().lower()

        self.tbl.setRowCount(0)
        self._visible_rows.clear()

        for i, rec in enumerate(self._rows):
            hay = f"{rec.id} {rec.name} {rec.url} {getattr(rec, 'epg_url', '')}".lower()
            if (not q) or (q in hay):
                self._visible_rows.append(i)

        self.tbl.setSortingEnabled(False)
        self.tbl.setRowCount(len(self._visible_rows))
        for r, src_i in enumerate(self._visible_rows):
            rec = self._rows[src_i]
            idx_item = QtWidgets.QTableWidgetItem(str(r + 1))
            idx_item.setData(QtCore.Qt.ItemDataRole.UserRole, rec.id)
            self.tbl.setItem(r, 0, idx_item)

            name_txt = rec.name or f"Playlist #{rec.id}"
            name_item = QtWidgets.QTableWidgetItem(name_txt)
            src_item = QtWidgets.QTableWidgetItem(rec.url)
            epg = getattr(rec, "epg_url", "") or ""
            if epg:
                src_item.setToolTip(f"EPG: {epg}")
            self.tbl.setItem(r, 1, name_item)
            self.tbl.setItem(r, 2, src_item)

        self.tbl.resizeColumnsToContents()
        self.tbl.setSortingEnabled(True)
        self._sel_changed()

    # ======================
    # Selection helpers
    # ======================
    def _sel_changed(self):
        has_sel = len(self.tbl.selectionModel().selectedRows()) > 0
        self.btn_load.setEnabled(has_sel)
        self.btn_edit_open.setEnabled(has_sel)
        self.btn_delete.setEnabled(has_sel)

    def _selected_pid(self) -> int | None:
        sel = self.tbl.selectionModel().selectedRows()
        if not sel:
            return None
        row = sel[0].row()
        item = self.tbl.item(row, 0)
        if not item:
            return None
        pid = item.data(QtCore.Qt.ItemDataRole.UserRole)
        try:
            return int(pid)
        except Exception:
            return None

    # ======================
    # Actions
    # ======================
    def _load_selected(self):
        pid = self._selected_pid()
        if pid is None:
            return
        self.quickload_requested.emit(pid)

    def _open_selected_in_editor(self):
        pid = self._selected_pid()
        if pid is None:
            return
        self.edit_requested.emit(pid)

    def _delete_selected(self):
        if not self.db:
            return

        pid = self._selected_pid()
        if pid is None:
            return

        row = self.tbl.selectionModel().selectedRows()[0].row()
        name = self.tbl.item(row, 1).text()

        msg = (
            f"Supprimer la playlist #{pid} (« {name} ») ?\n\n"
            "Les chaînes associées seront définitivement supprimées."
        )
        if QtWidgets.QMessageBox.question(self, "Supprimer", msg) != QtWidgets.QMessageBox.Yes:
            return

        try:
            self.db.delete_playlist(pid)
            self.log(f"Salon: playlist #{pid} supprimée.")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Erreur", str(e))
            return

        self.refresh()

    # ======================
    # Context menu
    # ======================
    def _context_menu(self, pos):
        pid = self._selected_pid()
        menu = QtWidgets.QMenu(self)

        act_load = menu.addAction("Charger dans le player")
        act_edit = menu.addAction("Ouvrir dans l’éditeur")
        act_del = menu.addAction("Supprimer")

        act_load.setEnabled(pid is not None)
        act_edit.setEnabled(pid is not None)
        act_del.setEnabled(pid is not None)

        act = menu.exec(self.tbl.viewport().mapToGlobal(pos))
        if act == act_load:
            self._load_selected()
        elif act == act_edit:
            self._open_selected_in_editor()
        elif act == act_del:
            self._delete_selected()
