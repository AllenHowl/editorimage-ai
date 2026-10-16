"""Ogni import del backend dev'essere dichiarato in pyproject.toml.

Un pacchetto presente sulla macchina di chi sviluppa ma assente dal wheel non
si vede finche' qualcuno non installa da PyPI.
"""

import ast
import sys
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
# nome di import -> nome del pacchetto su PyPI
ALIAS = {"PIL": "pillow", "cv2": "opencv-python-headless", "multipart": "python-multipart"}


def _dichiarati() -> set[str]:
    testo = (RADICE / "pyproject.toml").read_text(encoding="utf-8")
    nomi = set()
    for riga in testo.splitlines():
        riga = riga.strip().strip(",")
        if riga.startswith('"') and (">=" in riga or "[" in riga):
            nome = riga.strip('"').split(">=")[0].split("[")[0].strip().lower()
            nomi.add(nome)
    for gruppo in ("dev", "chat", "ai"):
        for riga in testo.splitlines():
            if riga.startswith(f"{gruppo} = ["):
                for parte in riga.split("[", 1)[1].rstrip("]").split(","):
                    if parte.strip():
                        nomi.add(parte.strip().strip('"').split(">=")[0].lower())
    return nomi


def test_imports_declared():
    dichiarati = _dichiarati()
    stdlib = set(sys.stdlib_module_names)
    mancanti = set()
    for f in (RADICE / "backend" / "pedit").glob("*.py"):
        for nodo in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(nodo, ast.Import):
                mods = [a.name for a in nodo.names]
            elif isinstance(nodo, ast.ImportFrom) and nodo.level == 0 and nodo.module:
                mods = [nodo.module]
            else:
                continue
            for m in mods:
                top = m.split(".")[0]
                if top in stdlib or top == "pedit":
                    continue
                if ALIAS.get(top, top).lower() not in dichiarati:
                    mancanti.add(f"{top} ({f.name})")
    assert not mancanti, f"imports not declared in pyproject.toml: {sorted(mancanti)}"
