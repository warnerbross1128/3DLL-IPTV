from __future__ import annotations

from PySide6 import QtGui, QtWidgets

THEME_NAME = "forest"


def build_palette() -> QtGui.QPalette:
    pal = QtWidgets.QStyleFactory.create("Fusion").standardPalette()
    pal.setColor(QtGui.QPalette.Window, QtGui.QColor(25, 45, 30))
    pal.setColor(QtGui.QPalette.Base, QtGui.QColor(35, 60, 40))
    pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(40, 70, 45))
    pal.setColor(QtGui.QPalette.Text, QtGui.QColor(220, 235, 220))
    pal.setColor(QtGui.QPalette.WindowText, QtGui.QColor(220, 235, 220))
    pal.setColor(QtGui.QPalette.Button, QtGui.QColor(40, 70, 45))
    pal.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(220, 235, 220))
    pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor(80, 170, 110))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(10, 25, 15))
    return pal
