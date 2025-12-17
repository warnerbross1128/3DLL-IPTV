from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from PySide6 import QtGui, QtWidgets


@dataclass
class ThemeSpec:
    name: str
    palette: QtGui.QPalette


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if not spec or not spec.loader:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    return mod


def discover_themes(theme_dir: Path | None = None) -> Dict[str, ThemeSpec]:
    """
    Charge dynamiquement chaque fichier de thème dans ui/themes/*.py (hors __init__.py).
    Chaque module doit exposer THEME_NAME et build_palette() -> QPalette.
    """
    theme_dir = theme_dir or Path(__file__).parent
    themes: Dict[str, ThemeSpec] = {}
    for p in sorted(theme_dir.glob("*.py")):
        if p.name == "__init__.py":
            continue
        mod = _load_module(p)
        if not mod:
            continue
        name = getattr(mod, "THEME_NAME", p.stem)
        build_palette = getattr(mod, "build_palette", None)
        if callable(build_palette):
            try:
                pal = build_palette()
                if isinstance(pal, QtGui.QPalette):
                    themes[name] = ThemeSpec(name=name, palette=pal)
            except Exception:
                continue
    if not themes:
        # Fallback minimal : palette Fusion claire
        base = QtWidgets.QStyleFactory.create("Fusion").standardPalette()
        themes["light"] = ThemeSpec("light", base)
    return themes


def theme_names(theme_dir: Path | None = None) -> List[str]:
    return list(discover_themes(theme_dir).keys())
