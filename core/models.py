from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Channel:
    extinf: str
    url: str
    name: str = ""
    group: str = ""
    tvg_id: str = ""
    status: str = "—"   # OK / KO / —
