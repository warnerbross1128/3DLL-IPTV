from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtWidgets


class SettingsTab(QtWidgets.QWidget):
    """
    Onglet de configuration : thème/style Qt et chemin EPG.
    Emet un signal de prévisualisation (sans sauvegarde) et un signal d'enregistrement.
    """

    config_preview = QtCore.Signal(dict)  # {theme, style, epg_path}
    config_changed = QtCore.Signal(dict)  # {theme, style, epg_path}

    def __init__(
        self,
        parent=None,
        themes: list[str] | None = None,
        initial_theme: str = "light",
        styles: list[str] | None = None,
        initial_style: str = "Fusion",
        initial_epg_path: str = "",
    ):
        super().__init__(parent)

        layout = QtWidgets.QVBoxLayout(self)

        # Sélecteur de thème / style
        group = QtWidgets.QGroupBox("Apparence")
        form = QtWidgets.QFormLayout(group)

        self.cmb_theme = QtWidgets.QComboBox()
        theme_list = themes or ["light", "dark", "ocean", "forest", "sunset", "retro"]
        self.cmb_theme.addItems(theme_list)
        if initial_theme in theme_list:
            self.cmb_theme.setCurrentText(initial_theme)

        self.cmb_style = QtWidgets.QComboBox()
        style_list = styles or ["Fusion", "Windows", "WindowsVista"]
        self.cmb_style.addItems(style_list)
        if initial_style in style_list:
            self.cmb_style.setCurrentText(initial_style)

        form.addRow("Thème", self.cmb_theme)
        form.addRow("Style Qt", self.cmb_style)
        layout.addWidget(group)

        # Emplacements utilisateur
        loc_group = QtWidgets.QGroupBox("Emplacements")
        loc_form = QtWidgets.QFormLayout(loc_group)

        epg_row = QtWidgets.QHBoxLayout()
        self.txt_epg_path = QtWidgets.QLineEdit(initial_epg_path or "")
        self.btn_epg_browse = QtWidgets.QToolButton(text="Parcourir...")
        epg_row.addWidget(self.txt_epg_path, 1)
        epg_row.addWidget(self.btn_epg_browse, 0)
        loc_form.addRow("Dossier EPG (npm/XML)", epg_row)

        layout.addWidget(loc_group)

        # Boutons
        btn_row = QtWidgets.QHBoxLayout()
        self.btn_save = QtWidgets.QPushButton("Enregistrer")
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_save)
        layout.addLayout(btn_row)
        layout.addStretch(1)

        self.cmb_theme.currentTextChanged.connect(self._emit_preview)
        self.cmb_style.currentTextChanged.connect(self._emit_preview)
        self.txt_epg_path.textChanged.connect(self._emit_preview)
        self.btn_epg_browse.clicked.connect(self._browse_epg_path)
        self.btn_save.clicked.connect(self._emit_save)

    def _emit_preview(self, *_):
        # Prévisualisation immédiate dans les widgets, l'enregistrement se fait via MainWindow au clic sur Enregistrer.
        payload = {
            "theme": self.cmb_theme.currentText(),
            "style": self.cmb_style.currentText(),
            "epg_path": self.txt_epg_path.text().strip(),
        }
        self.config_preview.emit(payload)

    def _emit_save(self):
        payload = {
            "theme": self.cmb_theme.currentText(),
            "style": self.cmb_style.currentText(),
            "epg_path": self.txt_epg_path.text().strip(),
        }
        self.config_changed.emit(payload)

    def _browse_epg_path(self):
        start_dir = self.txt_epg_path.text().strip() or str(Path.home())
        chosen = QtWidgets.QFileDialog.getExistingDirectory(self, "Choisir le dossier EPG", start_dir)
        if chosen:
            self.txt_epg_path.setText(chosen)
