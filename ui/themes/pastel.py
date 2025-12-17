from __future__ import annotations

from PySide6 import QtGui, QtWidgets

THEME_NAME = "pastel"


def build_palette() -> QtGui.QPalette:
    pal = QtWidgets.QStyleFactory.create("Fusion").standardPalette()
    pal.setColor(QtGui.QPalette.Window, QtGui.QColor("#f7f2f9"))
    pal.setColor(QtGui.QPalette.Base, QtGui.QColor("#ffffff"))
    pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor("#f0e8f2"))
    pal.setColor(QtGui.QPalette.Text, QtGui.QColor("#424242"))
    pal.setColor(QtGui.QPalette.WindowText, QtGui.QColor("#424242"))
    pal.setColor(QtGui.QPalette.Button, QtGui.QColor("#f0e8f2"))
    pal.setColor(QtGui.QPalette.ButtonText, QtGui.QColor("#424242"))
    pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#ffb3c1"))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor("#3a2f36"))
    return pal
