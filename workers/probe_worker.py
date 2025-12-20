from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

import requests
from PySide6 import QtCore

from core.models import Channel

# Worker Qt: teste la reachabilitЍ des URLs de chaЪnes (HEAD/GET rapide) via un pool bornЍ.


def _probe_url(url: str, timeout_s: float) -> str:
    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0", "Range": "bytes=0-1023"}

    try:
        r = session.head(url, allow_redirects=True, timeout=(2, 2))
        if r.status_code < 400:
            return f"OK (HEAD {r.status_code})"
    except Exception:
        pass

    try:
        r = session.get(url, headers=headers, allow_redirects=True, timeout=(timeout_s, timeout_s))
        if r.status_code < 400:
            return f"OK (GET {r.status_code})"
        return f"KO (GET {r.status_code})"
    except requests.exceptions.Timeout:
        return "KO (timeout)"
    except requests.exceptions.InvalidURL:
        return "KO (invalid url)"
    except Exception as e:
        return f"KO ({type(e).__name__})"


class ProbeWorker(QtCore.QObject):
    """Runs URL probes in a separate QThread, reporting status per channel row."""

    progress = QtCore.Signal(int, str)  # row, status
    progress_count = QtCore.Signal(int, int)  # done, total
    finished = QtCore.Signal()

    def __init__(self, channels: Iterable[Channel], timeout_s: float = 8.0, max_workers: int = 8):
        super().__init__()
        self.channels = list(channels)
        self.timeout_s = float(timeout_s)
        self.max_workers = max(1, int(max_workers))
        self._stop = False

    def stop(self):
        self._stop = True

    @QtCore.Slot()
    def run(self):
        """Boucle principale dЌclenchЌe dans un QThread parent."""
        total = len(self.channels)
        done = 0

        try:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_map = {}
                for idx, ch in enumerate(self.channels):
                    if self._stop:
                        break

                    url = (ch.url or "").strip()
                    if not url:
                        self.progress.emit(idx, "KO (no url)")
                        continue

                    fut = executor.submit(_probe_url, url, self.timeout_s)
                    future_map[fut] = idx

                for fut in as_completed(future_map):
                    if self._stop:
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    idx = future_map[fut]
                    try:
                        status = fut.result()
                    except Exception as e:
                        status = f"KO ({type(e).__name__})"
                    self.progress.emit(idx, status)
                    done += 1
                    self.progress_count.emit(done, total)

        except Exception as e:
            try:
                self.progress.emit(0, f"KO (worker exception: {type(e).__name__})")
            except Exception:
                pass
        finally:
            self.finished.emit()
