from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional


def _which_npm() -> str:
    """
    On Windows, npm peut être npm.cmd.
    """
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise RuntimeError(
            "npm introuvable. Installe Node.js (qui inclut npm), puis rouvre PowerShell.\n"
            "Astuce: dans PowerShell -> `npm -v` doit fonctionner."
        )
    return npm


def stream_process(cmd: list[str], cwd: Path, timeout_s: Optional[int] = None) -> int:
    """
    Lance une commande et affiche stdout/stderr en direct dans le terminal.
    """
    cwd = cwd.resolve()
    if not cwd.exists():
        raise FileNotFoundError(f"Dossier repo introuvable: {cwd}")

    # Sur Windows: éviter les soucis d'encodage
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")

    p = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )

    try:
        assert p.stdout is not None
        for line in p.stdout:
            print(line.rstrip("\n"))
        return p.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        p.kill()
        print(f"[TIMEOUT] Process tué après {timeout_s}s")
        return 124


def epg_grab(repo: str | Path, site: str, out_xml: str | Path, days: int = 1, timeout_s: int = 600) -> int:
    """
    Exécute: npm run grab -- --site=<site> --days=<days> --output=<out>
    """
    repo = Path(repo)
    npm = _which_npm()

    out_xml = Path(out_xml)
    # si out_xml est relatif, on le met dans le repo
    if not out_xml.is_absolute():
        out_xml = repo / out_xml

    cmd = [
        npm,
        "run",
        "grab",
        "--",
        f"--site={site}",
        f"--days={int(days)}",
        f"--output={str(out_xml)}",
    ]

    print(f"[RUN] cwd={repo}")
    print(f"[RUN] {' '.join(cmd)}")
    return stream_process(cmd, cwd=repo, timeout_s=int(timeout_s))


def list_sites_from_file(repo: str | Path) -> list[str]:
    """
    Lit SITES.md et extrait la liste des noms (ex: tvtv.us, tvpassport.com, tvhebdo.com...)
    """
    repo = Path(repo)
    sites_md = repo / "SITES.md"
    if not sites_md.exists():
        raise FileNotFoundError(f"SITES.md introuvable dans {repo}")

    sites: list[str] = []
    for line in sites_md.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("【") and "†" in line and "】" in line:
            # format vu sur GitHub raw parfois, mais en local c'est souvent markdown normal
            continue

    # Parsing simple du markdown local (lignes type: `- [tvtv.us](...) ...` selon versions)
    text = sites_md.read_text(encoding="utf-8", errors="ignore")
    for token in text.replace("(", " ").replace(")", " ").replace("|", " ").split():
        # on garde juste les trucs qui ressemblent à un domaine
        if "." in token and all(c.isalnum() or c in ".-" for c in token):
            # filtrer les faux positifs
            if token.lower() in ("site", "channels", "status", "notes"):
                continue
            if token.lower().endswith((".md", ".ts", ".js", ".xml")):
                continue
            # grossièrement: un domaine
            if token.count(".") >= 1 and len(token) <= 60:
                sites.append(token.strip())

    # dédoublonner en gardant l'ordre
    out: list[str] = []
    seen = set()
    for s in sites:
        s = s.strip().lower()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def find_site_for_tvg_id(repo: str | Path, tvg_id: str) -> list[Path]:
    """
    Cherche tvg_id dans ./sites/**/*.xml (utile pour retrouver le bon --site).
    Retourne une liste de fichiers où il apparaît.
    """
    repo = Path(repo)
    sites_dir = repo / "sites"
    if not sites_dir.exists():
        raise FileNotFoundError(f"Dossier sites introuvable: {sites_dir}")

    tvg_id = tvg_id.strip()
    hits: list[Path] = []
    for p in sites_dir.rglob("*.xml"):
        try:
            data = p.read_text(encoding="utf-8", errors="ignore")
            if tvg_id in data:
                hits.append(p)
        except Exception:
            pass
    return hits
