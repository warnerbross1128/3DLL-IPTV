from __future__ import annotations

from PySide6 import QtGui, QtWidgets

THEME_NAME = "monokai"


def build_palette() -> QtGui.QPalette:
    pal = QtWidgets.QStyleFactory.create("Fusion").standardPalette()
    bg = QtGui.QColor("#272822")
    base = QtGui.QColor("#1e1f1c")
    text = QtGui.QColor("#f8f8f2")
    highlight = QtGui.QColor("#66d9ef")
    pal.setColor(QtGui.QPalette.Window, bg)
    pal.setColor(QtGui.QPalette.Base, base)
    pal.setColor(QtGui.QPalette.AlternateBase, bg.darker(110))
    pal.setColor(QtGui.QPalette.Text, text)
    pal.setColor(QtGui.QPalette.WindowText, text)
    pal.setColor(QtGui.QPalette.Button, bg)
    pal.setColor(QtGui.QPalette.ButtonText, text)
    pal.setColor(QtGui.QPalette.Highlight, highlight)
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor("#000000"))
    return pal
