from __future__ import annotations

import multiprocessing as mp
from PySide6 import QtCore

from core.models import Channel


class ProbeWorker(QtCore.QObject):
    progress = QtCore.Signal(int, str)  # row, status
    finished = QtCore.Signal()

    def __init__(self, channels: list[Channel], timeout_s: float = 8.0):
        super().__init__()
        self.channels = channels
        self.timeout_s = float(timeout_s)
        self._stop = False

    def stop(self):
        self._stop = True

    @staticmethod
    def _probe_in_subprocess(url: str, timeout_s: float, q):
        import requests

        try:
            session = requests.Session()
            headers = {"User-Agent": "Mozilla/5.0", "Range": "bytes=0-1023"}

            try:
                r = session.head(url, allow_redirects=True, timeout=(2, 2))
                if r.status_code < 400:
                    q.put(f"OK (HEAD {r.status_code})")
                    return
            except Exception:
                pass

            try:
                r = session.get(url, headers=headers, allow_redirects=True, timeout=(timeout_s, timeout_s))
                if r.status_code < 400:
                    q.put(f"OK (GET {r.status_code})")
                else:
                    q.put(f"KO (GET {r.status_code})")
            except requests.exceptions.Timeout:
                q.put("KO (timeout)")
            except requests.exceptions.InvalidURL:
                q.put("KO (invalid url)")
            except Exception as e:
                q.put(f"KO ({type(e).__name__})")

        except Exception as e:
            q.put(f"KO (FATAL {type(e).__name__})")

    def _hard_probe(self, url: str) -> str:
        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        p = ctx.Process(target=ProbeWorker._probe_in_subprocess, args=(url, self.timeout_s, q))
        p.start()

        p.join(self.timeout_s + 1.0)

        if p.is_alive():
            p.terminate()
            p.join(1.0)
            return "KO (HARD TIMEOUT)"

        try:
            return q.get_nowait()
        except Exception:
            return "KO (no result)"

    @QtCore.Slot()
    def run(self):
        try:
            for idx, ch in enumerate(self.channels):
                if self._stop:
                    break

                url = (ch.url or "").strip()
                if not url:
                    self.progress.emit(idx, "KO (no url)")
                    continue

                status = self._hard_probe(url)
                self.progress.emit(idx, status)

        except Exception as e:
            try:
                self.progress.emit(0, f"KO (worker exception: {type(e).__name__})")
            except Exception:
                pass
        finally:
            self.finished.emit()
