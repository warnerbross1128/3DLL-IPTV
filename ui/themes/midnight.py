from __future__ import annotations

from PySide6 import QtGui, QtWidgets

THEME_NAME = "midnight"


def build_palette() -> QtGui.QPalette:
    pal = QtWidgets.QStyleFactory.create("Fusion").standardPalette()
    pal.setColor(QtGui.QPalette.Window, QtGui.QColor("#0f1a2b"))
    pal.setColor(QtGui.QPalette.Base, QtGui.QColor("#13233a"))
    pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor("#172a45"))
    pal.setColor(QtGui.QPalette.Text, QtGui.QColor("#dce6f2"))
    pal.setColor(QtGui.QPalette.WindowText, QtGui.QColor("#dce6f2"))
    pal.setColor(QtGui.QPalette.Button, QtGui.QColor("#13233a"))
    pal.setColor(QtGui.QPalette.ButtonText, QtGui.QColor("#dce6f2"))
    pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#3ea0e4"))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor("#0a1626"))
    return pal
