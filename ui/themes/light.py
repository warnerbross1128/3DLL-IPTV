from __future__ import annotations

from PySide6 import QtGui, QtWidgets

THEME_NAME = "light"


def build_palette() -> QtGui.QPalette:
    pal = QtWidgets.QStyleFactory.create("Fusion").standardPalette()
    # Palette explicite pour écraser toute influence du thème système.
    pal.setColor(QtGui.QPalette.Window, QtGui.QColor(245, 245, 245))
    pal.setColor(QtGui.QPalette.Base, QtGui.QColor(255, 255, 255))
    pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(245, 245, 245))
    pal.setColor(QtGui.QPalette.Text, QtGui.QColor(30, 30, 30))
    pal.setColor(QtGui.QPalette.WindowText, QtGui.QColor(30, 30, 30))
    pal.setColor(QtGui.QPalette.Button, QtGui.QColor(245, 245, 245))
    pal.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(30, 30, 30))
    pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor(76, 163, 224))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(255, 255, 255))
    return pal
