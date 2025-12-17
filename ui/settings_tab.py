from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets


class SettingsTab(QtWidgets.QWidget):
    """
    Onglet de configuration minimal : permet de choisir un thème clair/sombre via palette Qt.
    L'application applique le style en émettant un signal vers MainWindow.
    """

    theme_changed = QtCore.Signal(str)  # "light" / "dark"
    style_changed = QtCore.Signal(str)  # Qt style (Fusion/Windows/etc.)

    def __init__(self, parent=None, themes: list[str] | None = None, initial_theme: str = "light", styles: list[str] | None = None, initial_style: str = "Fusion"):
        super().__init__(parent)

        layout = QtWidgets.QVBoxLayout(self)

        # Sélecteur de thème
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
        layout.addStretch(1)

        self.cmb_theme.currentTextChanged.connect(self._on_theme_changed)
        self.cmb_style.currentTextChanged.connect(self._on_style_changed)

    def _on_theme_changed(self, val: str):
        self.theme_changed.emit(val)

    def _on_style_changed(self, val: str):
        self.style_changed.emit(val)
