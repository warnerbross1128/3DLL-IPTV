import sys
import vlc
from PySide6 import QtWidgets

class Test(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VLC test")

        self.video = QtWidgets.QFrame()
        self.setCentralWidget(self.video)

        self.instance = vlc.Instance("--no-xlib")
        self.player = self.instance.media_player_new()

        self.show()
        self.player.set_hwnd(self.video.winId())  # Windows
        self.player.set_media(self.instance.media_new(
            "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4"
        ))
        self.player.play()

app = QtWidgets.QApplication(sys.argv)
w = Test()
w.resize(800, 450)
w.show()
sys.exit(app.exec())
