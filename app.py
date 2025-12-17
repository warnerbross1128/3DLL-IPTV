from __future__ import annotations

import sys
import multiprocessing as mp
from PySide6 import QtWidgets

from ui.main_window import MainWindow

# Point d’entrée graphique : configure le multiprocessing en mode “spawn” (compatible
# PySide6/VLC sur Windows), instancie l’application Qt et affiche la fenêtre principale.


def main():
    mp.freeze_support()
    try:
        mp.set_start_method("spawn", force=True)
    except RuntimeError:
        pass

    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
