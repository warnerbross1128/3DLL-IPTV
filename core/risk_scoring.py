from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse

from .models import Channel

# Estimation de risque purement heuristique (offline) pour donner un signal visuel à l'utilisateur.

# Domain/TLD heuristics (deliberately static: no network calls).
SUSPICIOUS_TLDS = {
    "xyz",
    "tk",
    "top",
    "live",
    "club",
    "cam",
    "biz",
    "stream",
    "click",
    "pw",
    "best",
}

LOWER_RISK_TLDS = {"fr", "ca", "us", "uk", "de", "es", "it", "jp", "nl", "eu"}

LOWER_RISK_HOST_KEYWORDS = {"akamai", "akamaized", "cloudfront", "googlevideo", "llnwd", "canalplus"}
HIGHER_RISK_HOST_KEYWORDS = {"iptv", "freeip", "restream", "tvbox", "unofficial", "m3u", "panel"}
HIGHER_RISK_PATH_KEYWORDS = {"playlist", "restream", "rebroadcast", "adult", "xxx", "fullhd", "livehd", "hls", "ts"}
CATEGORY_RISK_KEYWORDS = {"24/7", "xxx", "adult", "ppv", "sports", "live"}

# Minimal country hints from ccTLD (not exhaustive, just for signal).
COUNTRY_TLD_MAP = {
    "fr": "FR",
    "ca": "CA",
    "us": "US",
    "uk": "UK",
    "de": "DE",
    "es": "ES",
    "it": "IT",
    "nl": "NL",
    "se": "SE",
    "no": "NO",
    "dk": "DK",
    "fi": "FI",
    "pt": "PT",
    "br": "BR",
    "ar": "AR",
    "cl": "CL",
    "mx": "MX",
    "ru": "RU",
}


@dataclass
class RiskAssessment:
    score: float
    badge: str
    level: str
    reasons: list[str]


def _normalize_score(raw: float) -> float:
    return max(0.0, min(100.0, raw))


def _badge_from_score(score: float) -> tuple[str, str]:
    if score < 34:
        return "🟢", "Faible"
    if score < 67:
        return "🟡", "Modéré"
    return "🔴", "Élevé"


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except Exception:
        return False


def _extract_country_hint(*values: str) -> str | None:
    """
    Rough extraction of a country hint:
      - last suffix in tvg-id (ex: tf1.fr -> FR)
      - 2-letter tokens in name/group (ex: [CA], (UK))
    """
    for val in values:
        if not val:
            continue
        # tvg-id or dotted suffix
        parts = re.split(r"[.\s\-_]+", val)
        if parts:
            last = parts[-1]
            if len(last) == 2 and last.isalpha():
                return last.upper()
        # explicit [XX] marker
        m = re.search(r"\b([A-Za-z]{2})\b", val)
        if m:
            return m.group(1).upper()
    return None


def assess_channel_risk(ch: Channel) -> RiskAssessment:
    """
    Stateless risk estimator: returns a 0-100 score + badge + reasons.
    It does NOT decide légal/illégal; it only surfaces signals the user can review.
    """
    url = (ch.url or "").strip()
    parsed = urlparse(url)
    score = 25.0  # neutral baseline
    reasons: list[str] = []

    # Scheme
    if not parsed.scheme:
        score += 35
        reasons.append("URL incomplète ou sans schéma.")
        return _finalize(score, reasons)  # can't go further
    if parsed.scheme not in {"http", "https"}:
        score += 10
        reasons.append(f"Schéma non standard ({parsed.scheme}).")
    if parsed.scheme == "http":
        score += 12
        reasons.append("Flux non chiffré (http).")

    host = (parsed.hostname or "").lower()
    tld = host.rsplit(".", 1)[-1] if "." in host else ""

    if not host:
        score += 25
        reasons.append("Hôte manquant dans l'URL.")
    elif _is_ip(host):
        score += 20
        reasons.append("Flux servi depuis une IP brute (pas de domaine).")
    else:
        if tld in SUSPICIOUS_TLDS:
            score += 12
            reasons.append(f"TLD fréquent sur flux non officiels ({tld}).")
        if tld in LOWER_RISK_TLDS:
            score -= 4
            reasons.append(f"TLD aligné sur pays courant ({tld}).")

        for kw in LOWER_RISK_HOST_KEYWORDS:
            if kw in host:
                score -= 6
                reasons.append(f"Hébergement CDN connu ({kw}).")
                break
        for kw in HIGHER_RISK_HOST_KEYWORDS:
            if kw in host:
                score += 8
                reasons.append(f"Mot-clé hôte indicatif de restream ({kw}).")
                break

    # Port
    if parsed.port and parsed.port not in {80, 443, 1935, 8080}:
        score += 6
        reasons.append(f"Port non standard ({parsed.port}).")

    # Path / filename hints
    path = (parsed.path or "").lower()
    for kw in HIGHER_RISK_PATH_KEYWORDS:
        if kw in path:
            score += 5
            reasons.append(f"Mot-clé chemin ({kw}).")
            break
    if path.endswith(".m3u8"):
        score -= 2
        reasons.append("Chemin HLS explicite (.m3u8).")

    # Channel metadata signals (category/type)
    name_lower = f"{ch.name} {ch.group}".lower()
    for kw in CATEGORY_RISK_KEYWORDS:
        if kw.lower() in name_lower:
            score += 6
            reasons.append(f"Libellé sensible ({kw}).")
            break

    # Geo consistency between tvg-id hint and host TLD
    country_hint = _extract_country_hint(ch.tvg_id, ch.group, ch.name)
    host_country = COUNTRY_TLD_MAP.get(tld)
    if country_hint and host_country:
        if country_hint != host_country:
            score += 5
            reasons.append(f"Hébergement {host_country} ≠ pays annoncé {country_hint}.")
        else:
            score -= 3
            reasons.append("Pays du flux cohérent avec l'identifiant de chaîne.")

    return _finalize(score, reasons)


def _finalize(raw_score: float, reasons: list[str]) -> RiskAssessment:
    score = _normalize_score(raw_score)
    badge, level = _badge_from_score(score)
    # Keep top 4 reasons to avoid noisy tooltips
    trimmed = reasons[:4]
    return RiskAssessment(score=score, badge=badge, level=level, reasons=trimmed)


def score_channels(channels: Iterable[Channel]) -> list[RiskAssessment]:
    """
    Helper to mutate Channel objects with risk info while returning the assessments.
    """
    assessments: list[RiskAssessment] = []
    for ch in channels:
        assessment = assess_channel_risk(ch)
        ch.risk_score = assessment.score
        ch.risk_level = assessment.level
        ch.risk_badge = assessment.badge
        ch.risk_reasons = " • ".join(assessment.reasons)
        assessments.append(assessment)
    return assessments
