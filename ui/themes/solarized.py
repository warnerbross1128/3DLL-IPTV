from __future__ import annotations

from PySide6 import QtGui, QtWidgets

THEME_NAME = "solarized"


def build_palette() -> QtGui.QPalette:
    pal = QtWidgets.QStyleFactory.create("Fusion").standardPalette()
    base_bg = QtGui.QColor("#fdf6e3")
    alt_bg = QtGui.QColor("#f5e9d0")
    text = QtGui.QColor("#586e75")
    highlight = QtGui.QColor("#268bd2")
    pal.setColor(QtGui.QPalette.Window, base_bg)
    pal.setColor(QtGui.QPalette.Base, QtGui.QColor("#fffdf6"))
    pal.setColor(QtGui.QPalette.AlternateBase, alt_bg)
    pal.setColor(QtGui.QPalette.Text, text)
    pal.setColor(QtGui.QPalette.WindowText, text)
    pal.setColor(QtGui.QPalette.Button, base_bg)
    pal.setColor(QtGui.QPalette.ButtonText, text)
    pal.setColor(QtGui.QPalette.Highlight, highlight)
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor("#fdf6e3"))
    return pal
