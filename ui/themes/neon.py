from __future__ import annotations

from PySide6 import QtGui, QtWidgets

THEME_NAME = "neon"


def build_palette() -> QtGui.QPalette:
    pal = QtWidgets.QStyleFactory.create("Fusion").standardPalette()
    pal.setColor(QtGui.QPalette.Window, QtGui.QColor("#0b0c10"))
    pal.setColor(QtGui.QPalette.Base, QtGui.QColor("#0f111a"))
    pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor("#131524"))
    pal.setColor(QtGui.QPalette.Text, QtGui.QColor("#c5c6c7"))
    pal.setColor(QtGui.QPalette.WindowText, QtGui.QColor("#c5c6c7"))
    pal.setColor(QtGui.QPalette.Button, QtGui.QColor("#0f111a"))
    pal.setColor(QtGui.QPalette.ButtonText, QtGui.QColor("#c5c6c7"))
    pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#66fcf1"))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor("#0b0c10"))
    return pal
