from __future__ import annotations

import re
from pathlib import Path
from typing import List

from .models import Channel


EXTINF_NAME_RE = re.compile(r",\s*(.*)$")
ATTR_RE = re.compile(r'(\w[\w\-]*)="([^"]*)"')


def parse_extinf(extinf: str) -> dict:
    attrs = dict(ATTR_RE.findall(extinf))
    m = EXTINF_NAME_RE.search(extinf)
    name = (m.group(1).strip() if m else "").strip()
    return {"name": name, "group": attrs.get("group-title", ""), "tvg_id": attrs.get("tvg-id", "")}


def parse_m3u(text: str) -> List[Channel]:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out: List[Channel] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("#EXTINF"):
            extinf = lines[i]
            url = ""
            if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                url = lines[i + 1]
            meta = parse_extinf(extinf)
            out.append(Channel(
                extinf=extinf,
                url=url,
                name=meta["name"],
                group=meta["group"],
                tvg_id=meta["tvg_id"],
            ))
            i += 2
        else:
            i += 1
    return out


def write_m3u(channels: List[Channel], path: Path):
    with path.open("w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for ch in channels:
            if ch.url:
                f.write(ch.extinf + "\n")
                f.write(ch.url + "\n")
