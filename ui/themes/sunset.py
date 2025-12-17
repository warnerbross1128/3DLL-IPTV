from __future__ import annotations

from PySide6 import QtGui, QtWidgets

THEME_NAME = "sunset"


def build_palette() -> QtGui.QPalette:
    pal = QtWidgets.QStyleFactory.create("Fusion").standardPalette()
    pal.setColor(QtGui.QPalette.Window, QtGui.QColor(55, 35, 45))
    pal.setColor(QtGui.QPalette.Base, QtGui.QColor(75, 45, 55))
    pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(85, 55, 65))
    pal.setColor(QtGui.QPalette.Text, QtGui.QColor(245, 230, 225))
    pal.setColor(QtGui.QPalette.WindowText, QtGui.QColor(245, 230, 225))
    pal.setColor(QtGui.QPalette.Button, QtGui.QColor(85, 55, 65))
    pal.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(245, 230, 225))
    pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor(255, 140, 100))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(35, 15, 10))
    return pal
