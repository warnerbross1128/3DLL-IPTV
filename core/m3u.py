from __future__ import annotations

import re
from pathlib import Path
from typing import List

from .models import Channel

# Parsing/écriture minimalistes pour les playlists M3U (EXTINF + URL).

EXTINF_NAME_RE = re.compile(r",\s*(.*)$")
ATTR_RE = re.compile(r'(\w[\w\-]*)="([^"]*)"')
EXTVLCOPT_PREFIX = "#EXTVLCOPT:"


def parse_extinf(extinf: str) -> dict:
    """Extrait nom + attributs connus (groupe, tvg-id) depuis une ligne #EXTINF."""
    attrs = dict(ATTR_RE.findall(extinf))
    m = EXTINF_NAME_RE.search(extinf)
    name = (m.group(1).strip() if m else "").strip()
    return {"name": name, "group": attrs.get("group-title", ""), "tvg_id": attrs.get("tvg-id", "")}


def parse_m3u(text: str) -> List[Channel]:
    """
    Convertit le texte M3U en objets Channel.
    Supporte les options VLC via des lignes `#EXTVLCOPT:...` entre `#EXTINF` et l'URL.
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out: List[Channel] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("#EXTINF"):
            extinf = lines[i]

            vlc_opts: list[str] = []
            url = ""

            # La ligne URL n'est pas forcément juste après EXTINF (peut y avoir EXTVLCOPT, etc.).
            j = i + 1
            while j < len(lines) and lines[j].startswith("#"):
                if lines[j].upper().startswith(EXTVLCOPT_PREFIX):
                    opt = lines[j].split(":", 1)[1].strip()
                    if opt:
                        vlc_opts.append(opt)
                j += 1

            if j < len(lines) and not lines[j].startswith("#"):
                url = lines[j]
                j += 1
            meta = parse_extinf(extinf)
            out.append(Channel(
                extinf=extinf,
                url=url,
                name=meta["name"],
                group=meta["group"],
                tvg_id=meta["tvg_id"],
                vlc_opts=vlc_opts,
            ))
            i = j
        else:
            i += 1
    return out


def write_m3u(channels: List[Channel], path: Path):
    """Écrit une playlist M3U minimale à partir d'une liste de Channel."""
    with path.open("w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for ch in channels:
            if ch.url:
                f.write(ch.extinf + "\n")
                for opt in getattr(ch, "vlc_opts", []) or []:
                    opt = str(opt).strip()
                    if opt:
                        f.write(f"{EXTVLCOPT_PREFIX}{opt}\n")
                f.write(ch.url + "\n")
