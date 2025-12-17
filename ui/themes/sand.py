from __future__ import annotations

from PySide6 import QtGui, QtWidgets

THEME_NAME = "sand"


def build_palette() -> QtGui.QPalette:
    pal = QtWidgets.QStyleFactory.create("Fusion").standardPalette()
    pal.setColor(QtGui.QPalette.Window, QtGui.QColor("#f4e9d7"))
    pal.setColor(QtGui.QPalette.Base, QtGui.QColor("#fff7eb"))
    pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor("#f0e2cc"))
    pal.setColor(QtGui.QPalette.Text, QtGui.QColor("#5a4a36"))
    pal.setColor(QtGui.QPalette.WindowText, QtGui.QColor("#5a4a36"))
    pal.setColor(QtGui.QPalette.Button, QtGui.QColor("#f0e2cc"))
    pal.setColor(QtGui.QPalette.ButtonText, QtGui.QColor("#5a4a36"))
    pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#d4a15a"))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor("#2f2418"))
    return pal
