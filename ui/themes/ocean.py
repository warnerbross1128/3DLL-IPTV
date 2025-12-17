from __future__ import annotations

from PySide6 import QtGui, QtWidgets

THEME_NAME = "ocean"


def build_palette() -> QtGui.QPalette:
    pal = QtWidgets.QStyleFactory.create("Fusion").standardPalette()
    pal.setColor(QtGui.QPalette.Window, QtGui.QColor(15, 38, 55))
    pal.setColor(QtGui.QPalette.Base, QtGui.QColor(25, 50, 70))
    pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(30, 60, 80))
    pal.setColor(QtGui.QPalette.Text, QtGui.QColor(225, 238, 245))
    pal.setColor(QtGui.QPalette.WindowText, QtGui.QColor(225, 238, 245))
    pal.setColor(QtGui.QPalette.Button, QtGui.QColor(20, 60, 90))
    pal.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(225, 238, 245))
    pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor(30, 150, 200))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(5, 20, 30))
    return pal
