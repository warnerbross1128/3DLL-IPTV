# epg_npm_bridge.py
from __future__ import annotations

import os
import subprocess
import tempfile
import shutil
from pathlib import Path
from typing import Callable, Iterable
import xml.etree.ElementTree as ET

# Pont avec le repo iptv-org/epg (npm): sélectionne les sites utiles, lance `npm run grab` et fusionne les guides.
LogFn = Callable[[str], None]


def _default_log(msg: str) -> None:
    print(msg, flush=True)


def _canonical_id(value: str) -> str:
    """
    Clé canonique pour faire correspondre playlist tvg-id <-> xmltv_id du repo EPG.
    On ignore les suffixes '@...' (souvent @SD/@HD dans iptv-org/epg) et on compare en case-insensitive.
    """
    v = (value or "").strip()
    if not v:
        return ""
    base = v.split("@", 1)[0].strip()
    return base.casefold()


def _wanted_map(tvg_ids: Iterable[str]) -> dict[str, str]:
    """
    Retourne {canonical_id: tvg_id_original} (le tvg_id_original est celui de la playlist).
    """
    out: dict[str, str] = {}
    for t in tvg_ids:
        t = (t or "").strip()
        if not t:
            continue
        k = _canonical_id(t)
        if k and k not in out:
            out[k] = t
    return out


def _quality_rank(xmltv_id: str) -> int:
    x = (xmltv_id or "").upper()
    if "@HD" in x:
        return 2
    if "@SD" in x:
        return 1
    return 0


def _env_int(name: str, default: int) -> int:
    v = (os.environ.get(name) or "").strip()
    if not v:
        return default
    try:
        return int(v)
    except Exception:
        return default


def _which_npm() -> str:
    """
    Windows: npm est souvent npm.cmd (fichier batch)
    """
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise RuntimeError(
            "npm introuvable.\n"
            "Installe Node.js (inclut npm) puis REDÉMARRE Windows.\n"
            "Test PowerShell: npm -v"
        )
    return npm


def find_sites_for_tvg_ids(repo: str | Path, tvg_ids: Iterable[str], log: LogFn | None = None) -> list[str]:
    """
    Scanne epg/sites/**/**.channels.xml et retourne les sites dont les channels.xml
    contiennent au moins un des tvg-id demandés.
    """
    log = log or _default_log
    repo = Path(repo)
    sites_dir = repo / "sites"
    if not sites_dir.exists():
        raise FileNotFoundError(f"Repo EPG invalide: {sites_dir} introuvable")

    wanted = _wanted_map(tvg_ids)
    if not wanted:
        return []
    wanted_keys = set(wanted.keys())

    coverage: dict[str, set[str]] = {}
    for ch_xml in sites_dir.glob("*/*.channels.xml"):
        site = ch_xml.parent.name
        try:
            tree = ET.parse(str(ch_xml))
            root = tree.getroot()
        except Exception:
            continue

        matched: set[str] = set()
        for ch in root.findall("channel"):
            xmltv_id = (ch.attrib.get("xmltv_id") or "").strip()
            k = _canonical_id(xmltv_id)
            if k and k in wanted_keys:
                matched.add(k)
        if matched:
            coverage[site] = matched

    if not coverage:
        log("[EPG] Aucun site trouvé pour ces tvg-id.")
        return []

    candidates = sorted(coverage.keys(), key=lambda s: len(coverage[s]), reverse=True)
    log(f"[EPG] Sites candidats: {candidates}")

    # Réduction: greedy set cover pour éviter de lancer 50+ sites quand un seul en couvre déjà la majorité.
    remaining = set(wanted_keys)
    selected: list[str] = []
    max_sites = _env_int("IPTV_EPG_MAX_SITES", 12)
    if max_sites <= 0:
        log("[EPG] IPTV_EPG_MAX_SITES<=0 -> pas de limite, tous les sites candidats seront tentés.")
        return candidates
    while remaining and len(selected) < max_sites:
        best_site = ""
        best_gain = 0
        for s in candidates:
            if s in selected:
                continue
            gain = len(coverage[s] & remaining)
            if gain > best_gain:
                best_gain = gain
                best_site = s
        if best_gain == 0:
            break
        selected.append(best_site)
        remaining -= coverage[best_site]

    covered = len(wanted_keys) - len(remaining)
    total = len(wanted_keys)
    pct = int((covered / total) * 100) if total else 0
    log(f"[EPG] Sites sélectionnés ({len(selected)}): {selected} (couverture {covered}/{total} ~{pct}%)")
    if remaining:
        sample = [wanted[k] for k in list(sorted(remaining))[:12] if k in wanted]
        if sample:
            log(f"[EPG] Non couverts (exemples): {sample}")

    return selected


def _build_npm_command(npm_path: str, args: list[str]) -> list[str]:
    """
    IMPORTANT Windows:
    - si npm_path est un .cmd, CreateProcess ne l'exécute pas directement.
      On passe donc par: cmd.exe /c npm.cmd ...
    """
    npm_lower = npm_path.lower()
    if npm_lower.endswith(".cmd") or npm_lower.endswith(".bat"):
        return ["cmd.exe", "/c", npm_path, *args]
    return [npm_path, *args]


def npm_grab_site(
    repo: str | Path,
    site: str | None,
    days: int,
    out_xml: str | Path,
    timeout_s: int = 900,
    channels_path: str | Path | None = None,
    lang: str | None = None,
    max_connections: int | None = None,
    req_timeout_ms: int | None = None,
    log: LogFn | None = None,
) -> None:
    log = log or _default_log
    repo = Path(repo)
    out_xml = Path(out_xml)
    out_xml.parent.mkdir(parents=True, exist_ok=True)

    npm = _which_npm()

    npm_args = ["run", "grab", "--"]

    if channels_path:
        npm_args.append(f"--channels={str(Path(channels_path))}")
    elif site:
        npm_args.append(f"--site={site}")
    else:
        raise ValueError("npm_grab_site: il faut soit site, soit channels_path")

    npm_args.append(f"--days={int(days)}")
    npm_args.append(f"--output={str(out_xml)}")

    if lang:
        npm_args.append(f"--lang={lang}")

    if max_connections is not None:
        npm_args.append(f"--maxConnections={int(max_connections)}")

    if req_timeout_ms is not None:
        npm_args.append(f"--timeout={int(req_timeout_ms)}")

    cmd = _build_npm_command(npm, npm_args)
    log(f"[EPG] RUN: {' '.join(cmd)} (cwd={repo})")

    p = subprocess.Popen(
        cmd,
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        universal_newlines=True,
        bufsize=1,
        env=os.environ.copy(),
    )

    try:
        assert p.stdout is not None
        for line in p.stdout:
            line = line.rstrip("\n")
            if line:
                log(f"[npm:{site or 'channels'}] {line}")
        rc = p.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        p.kill()
        raise TimeoutError(f"npm grab timeout ({timeout_s}s)")
    finally:
        try:
            if p.stdout:
                p.stdout.close()
        except Exception:
            pass

    if rc != 0:
        raise RuntimeError(f"npm grab a échoué (code={rc})")

    if (not out_xml.exists()) or out_xml.stat().st_size == 0:
        raise RuntimeError(f"npm grab terminé, mais XML manquant/vide: {out_xml}")


def merge_xmltv(files: list[str | Path], out_xml: str | Path, log: LogFn | None = None) -> Path:
    """
    Merge simple XMLTV:
    - dédupe <channel id="...">
    - concatène tous les <programme>
    """
    log = log or _default_log
    out_xml = Path(out_xml)
    out_xml.parent.mkdir(parents=True, exist_ok=True)

    root = ET.Element("tv")
    seen_channels: set[str] = set()
    prog_count = 0
    chan_count = 0

    for f in files:
        f = Path(f)
        if not f.exists() or f.stat().st_size == 0:
            continue

        tree = ET.parse(str(f))
        r = tree.getroot()

        for ch in r.findall("channel"):
            cid = (ch.attrib.get("id") or "").strip()
            if not cid or cid in seen_channels:
                continue
            seen_channels.add(cid)
            root.append(ch)
            chan_count += 1

        for pr in r.findall("programme"):
            root.append(pr)
            prog_count += 1

    ET.ElementTree(root).write(str(out_xml), encoding="utf-8", xml_declaration=True)
    log(f"[EPG] Merge OK: channels={chan_count}, programmes={prog_count} -> {out_xml}")
    return out_xml


def build_custom_channels_xml(repo: str | Path, site: str, tvg_ids: Iterable[str], out_path: str | Path) -> Path:
    """
    Lit: sites/<site>/<site>.channels.xml
    Et écrit un custom.channels.xml contenant seulement les channels dont xmltv_id == tvg-id demandé.
    """
    repo = Path(repo)
    out_path = Path(out_path)
    wanted = _wanted_map(tvg_ids)
    if not wanted:
        raise RuntimeError("Aucun tvg-id fourni")
    wanted_keys = set(wanted.keys())

    src = repo / "sites" / site / f"{site}.channels.xml"
    if not src.exists():
        raise FileNotFoundError(f"Fichier channels introuvable: {src}")

    tree = ET.parse(str(src))
    root = tree.getroot()

    out_root = ET.Element("channels")
    kept = 0

    best: dict[str, tuple[int, ET.Element]] = {}
    for ch in root.findall("channel"):
        xmltv_id = (ch.attrib.get("xmltv_id") or "").strip()
        k = _canonical_id(xmltv_id)
        if not k or k not in wanted_keys:
            continue
        rank = _quality_rank(xmltv_id)
        cur = best.get(k)
        if cur is None or rank > cur[0]:
            best[k] = (rank, ch)

    for k, (_, ch) in best.items():
        # Copie de l'élément pour pouvoir réécrire xmltv_id sans toucher au fichier source.
        attrib = dict(ch.attrib)
        attrib["xmltv_id"] = wanted[k]
        out_ch = ET.Element("channel", attrib=attrib)
        out_ch.text = ch.text
        out_root.append(out_ch)
        kept += 1

    if kept == 0:
        raise RuntimeError(
            f"Aucun channel match dans {src} pour ces tvg-id "
            f"(astuce: xmltv_id peut contenir @SD/@HD)."
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(out_root).write(str(out_path), encoding="utf-8", xml_declaration=True)
    return out_path


def generate_xmltv_for_tvg_ids(
    repo: str | Path,
    tvg_ids: Iterable[str],
    days: int = 1,
    timeout_s: int = 900,
    log: LogFn | None = None,
) -> bytes:
    """Pipeline npm: filtre sites par tvg-id, génère des channels dédiés, lance grab, puis fusionne."""
    """
    Pipeline:
    1) trouve les sites pertinents (via *.channels.xml)
    2) construit un custom.channels.xml pour chaque site (uniquement tes tvg-id)
    3) npm grab avec --channels
    4) merge
    """
    log = log or _default_log
    repo = Path(repo)

    sites = find_sites_for_tvg_ids(repo, tvg_ids, log=log)
    if not sites:
        raise RuntimeError("Aucun --site trouvé pour tes tvg-id. (tvg-id pas couvert par iptv-org/epg)")

    with tempfile.TemporaryDirectory(prefix="epg_grab_") as td:
        td = Path(td)
        grabbed: list[Path] = []
        skipped: list[str] = []

        for s in sites:
            # IMPORTANT: ne jamais planter si un site est incomplet (ex: ontvtonight.com sans channels.xml local)
            try:
                custom_channels = td / f"{s}.custom.channels.xml"
                build_custom_channels_xml(repo, s, tvg_ids, custom_channels)
                log(f"[EPG] custom channels: {custom_channels.name}")

                out = td / f"{s}.xml"
                npm_grab_site(
                    repo=repo,
                    site=s,  # juste pour le label de log
                    days=days,
                    out_xml=out,
                    timeout_s=timeout_s,
                    channels_path=custom_channels,
                    max_connections=3,
                    req_timeout_ms=5000,
                    log=log,
                )
                grabbed.append(out)

            except FileNotFoundError as e:
                skipped.append(s)
                log(f"[EPG] SKIP {s}: {e}")
            except RuntimeError as e:
                # ex: "Aucun channel match" => skip ce site (pas utile)
                skipped.append(s)
                log(f"[EPG] SKIP {s}: {e}")

        if not grabbed:
            msg = "Aucun site n'a produit de guide XML (tous SKIP/KO)."
            if skipped:
                msg += f" Sites SKIP: {', '.join(skipped)}"
            raise RuntimeError(msg)

        merged = td / "guide.merged.xml"
        merge_xmltv(grabbed, merged, log=log)
        if skipped:
            log(f"[EPG] Note: sites ignorés: {', '.join(skipped)}")
        return merged.read_bytes()
