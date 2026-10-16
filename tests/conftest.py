"""Fixture comuni: immagini sintetiche, documenti in cartelle temporanee.

Niente rete, niente modelli: ``PEDIT_HOME`` punta a una cartella temporanea
cosi' i test non toccano ``~/.pedit`` di chi li lancia.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from pedit.store import Store


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("PEDIT_HOME", str(tmp_path / "home"))
    # porta chiusa: ComfyUI "spento"
    monkeypatch.setenv("PEDIT_COMFY_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("PEDIT_SEGNALAZIONI", str(tmp_path / "SEGNALAZIONI.md"))


@pytest.fixture
def photo(tmp_path):
    """Foto finta: gradiente orizzontale con un quadrato giallo in mezzo."""
    h, w = 120, 240
    arr = np.zeros((h, w, 3), np.uint8)
    arr[..., 0] = np.linspace(0, 255, w)[None, :]
    arr[..., 2] = 120
    arr[40:80, 100:140] = [250, 230, 20]
    p = tmp_path / "photo.png"
    Image.fromarray(arr).save(p)
    return str(p)


@pytest.fixture
def store(tmp_path):
    return Store.create(name="t", width=400, height=200, background="#000000", path=str(tmp_path / "doc.json"))


def px(img, x, y):
    return img.convert("RGBA").getpixel((x, y))
