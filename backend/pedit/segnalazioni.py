"""Registro di cio' che non va, scritto dagli agenti mentre lavorano.

``report_issue`` aggiunge una voce a ``SEGNALAZIONI.md``: alla radice della repo
se il pacchetto gira da un clone, altrimenti in ``~/.pedit/SEGNALAZIONI.md``
(``PEDIT_SEGNALAZIONI`` lo sposta). E' la lista delle cose da migliorare.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

TIPI = ("bug", "scomodo", "sbagliato", "manca", "lento")


def file_segnalazioni() -> Path:
    env = os.environ.get("PEDIT_SEGNALAZIONI")
    if env:
        return Path(env)
    radice = Path(__file__).resolve().parents[2]
    if (radice / "pyproject.toml").is_file() and (radice / "backend" / "pedit").is_dir():
        return radice / "SEGNALAZIONI.md"
    base = Path(os.environ.get("PEDIT_HOME") or (Path.home() / ".pedit"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "SEGNALAZIONI.md"


def aggiungi(tipo: str, titolo: str, dettagli: str = "", strumento: str = "") -> dict:
    if tipo not in TIPI:
        raise ValueError(f"kind must be one of {', '.join(TIPI)}")
    if not titolo.strip():
        raise ValueError("title is empty")
    f = file_segnalazioni()
    nuovo = not f.exists()
    righe = []
    if nuovo:
        righe.append("# Segnalazioni\n\nCio' che gli agenti hanno trovato rotto, scomodo o mancante. "
                     "Chi sistema una voce la cancella.\n")
    quando = time.strftime("%Y-%m-%d %H:%M")
    righe.append(f"\n## [{tipo}] {titolo.strip()}\n\n- quando: {quando}\n"
                 + (f"- strumento: `{strumento}`\n" if strumento else "")
                 + (f"\n{dettagli.strip()}\n" if dettagli.strip() else ""))
    with f.open("a", encoding="utf-8") as fh:
        fh.write("".join(righe))
    return {"saved": str(f)}
