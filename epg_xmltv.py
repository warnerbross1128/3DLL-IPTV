# epg_xmltv.py
from __future__ import annotations

import gzip
import io
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Iterable

import requests


"""
Outils XMLTV: téléchargement d'un guide (XML/ZIP) puis parsing en flux pour insertion en DB.
"""

_DT_RE = re.compile(r"^(\d{14})")  # YYYYMMDDHHMMSS


def download_xmltv(url: str, timeout: int = 90) -> bytes:
    """Télécharge un flux XMLTV (support .gz) et renvoie les bytes décompressés."""
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    data = r.content

    # .gz support
    if url.lower().endswith(".gz") or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)

    return data


def _parse_xmltv_dt(s: str) -> int:
    """
    XMLTV: "20240101060000 +0000" ou "20240101060000 -0500" ou "20240101060000"
    Retour: unix seconds UTC
    """
    if not s:
        return 0

    s = s.strip()
    m = _DT_RE.match(s)
    if not m:
        return 0

    dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")

    # timezone optionnel: " +HHMM" / " -HHMM"
    parts = s.split()
    if len(parts) >= 2 and (parts[1].startswith("+") or parts[1].startswith("-")) and len(parts[1]) == 5:
        off = parts[1]
        sign = 1 if off[0] == "+" else -1
        hh = int(off[1:3])
        mm = int(off[3:5])
        offset_seconds = sign * (hh * 3600 + mm * 60)
        tz = timezone(timedelta(seconds=offset_seconds))
        dt = dt.replace(tzinfo=tz)
        return int(dt.timestamp())

    # sinon, on assume UTC
    dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def iter_programs(xml_bytes: bytes) -> Iterable[dict]:
    """
    Yields dicts: {tvg_id, start_ts, stop_ts, title, desc}
    Utilise iterparse pour gros guides.
    """
    f = io.BytesIO(xml_bytes)
    context = ET.iterparse(f, events=("end",))

    for _, elem in context:
        if elem.tag != "programme":
            continue

        tvg_id = (elem.attrib.get("channel") or "").strip()
        start_ts = _parse_xmltv_dt(elem.attrib.get("start", ""))
        stop_ts = _parse_xmltv_dt(elem.attrib.get("stop", ""))

        title = ""
        desc = ""

        t = elem.find("title")
        if t is not None and t.text:
            title = t.text.strip()

        d = elem.find("desc")
        if d is not None and d.text:
            desc = d.text.strip()

        if tvg_id and start_ts and stop_ts:
            yield {"tvg_id": tvg_id, "start_ts": start_ts, "stop_ts": stop_ts, "title": title, "desc": desc}

        elem.clear()
