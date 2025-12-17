from __future__ import annotations

from PySide6 import QtGui, QtWidgets

THEME_NAME = "retro"


def build_palette() -> QtGui.QPalette:
    pal = QtWidgets.QStyleFactory.create("Fusion").standardPalette()
    pal.setColor(QtGui.QPalette.Window, QtGui.QColor(250, 245, 230))
    pal.setColor(QtGui.QPalette.Base, QtGui.QColor(235, 225, 210))
    pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(240, 230, 215))
    pal.setColor(QtGui.QPalette.Text, QtGui.QColor(45, 40, 35))
    pal.setColor(QtGui.QPalette.WindowText, QtGui.QColor(45, 40, 35))
    pal.setColor(QtGui.QPalette.Button, QtGui.QColor(230, 215, 195))
    pal.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(45, 40, 35))
    pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor(200, 140, 60))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(20, 15, 10))
    return pal
