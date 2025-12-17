from __future__ import annotations

from PySide6 import QtGui, QtWidgets

THEME_NAME = "dark"


def build_palette() -> QtGui.QPalette:
    pal = QtWidgets.QStyleFactory.create("Fusion").standardPalette()
    pal.setColor(QtGui.QPalette.Window, QtGui.QColor(45, 45, 45))
    pal.setColor(QtGui.QPalette.Base, QtGui.QColor(35, 35, 35))
    pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(40, 40, 40))
    pal.setColor(QtGui.QPalette.Text, QtGui.QColor(230, 230, 230))
    pal.setColor(QtGui.QPalette.WindowText, QtGui.QColor(230, 230, 230))
    pal.setColor(QtGui.QPalette.Button, QtGui.QColor(55, 55, 55))
    pal.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(230, 230, 230))
    pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor(90, 140, 255))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(0, 0, 0))
    return pal
