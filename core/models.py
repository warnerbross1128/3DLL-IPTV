from __future__ import annotations
from dataclasses import dataclass

# Structures de données partagées entre UI, workers et stockage.


@dataclass
class Channel:
    """Représente une entrée M3U/playlist enrichie d'un scoring de risque."""
    extinf: str
    url: str
    name: str = ""
    group: str = ""
    tvg_id: str = ""
    status: str = "—"  # OK / KO / —
    # Scoring indicatif (aucune décision automatique)
    risk_score: float = 0.0
    risk_level: str = "Inconnu"
    risk_badge: str = "🟢"
    risk_reasons: str = ""
