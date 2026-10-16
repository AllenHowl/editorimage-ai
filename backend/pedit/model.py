"""Modello dati del documento.

Un documento e' JSON puro: nessuno stato nascosto, tutto serializzabile. Il
render e' una funzione pura del documento (piu' i file degli asset), quindi lo
stesso documento produce sempre gli stessi pixel.

Niente e' distruttivo: un filtro e' una voce in ``effects``, una maschera e' un
campo del livello, perfino lo scontorno IA e' una maschera salvata come asset a
parte. L'immagine originale non viene mai riscritta.

Coordinate: pixel del documento, origine in alto a sinistra. ``x``/``y`` di un
livello sono l'angolo in alto a sinistra del suo contenuto *prima* della
rotazione, che avviene attorno al centro del contenuto scalato.
"""

from __future__ import annotations

import types
import uuid
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Union, get_args, get_origin, get_type_hints

from PIL import features as _features

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
# AVIF dentro Pillow solo dalla 11.3, e solo se la ruota ha libavif
if _features.check("avif"):
    IMAGE_EXTS.add(".avif")

LAYER_TYPES = ("image", "text", "shape", "fill", "adjustment", "paint", "group")

# le 27 di Photoshop (``add`` e' il suo Linear Dodge) piu' ``pass_through``,
# che vale solo per i gruppi: i figli si fondono direttamente con cio' che sta
# sotto il gruppo, come se il gruppo non ci fosse
BLEND_MODES = (
    "normal", "dissolve",
    "darken", "multiply", "color_burn", "linear_burn", "darker_color",
    "lighten", "screen", "color_dodge", "add", "lighter_color",
    "overlay", "soft_light", "hard_light", "vivid_light", "linear_light", "pin_light", "hard_mix",
    "difference", "exclusion", "subtract", "divide",
    "hue", "saturation", "color", "luminosity",
)
GROUP_BLEND_MODES = ("pass_through",) + BLEND_MODES

SHAPE_KINDS = ("rect", "ellipse", "line", "polygon")
FILL_KINDS = ("solid", "linear", "radial")
MASK_KINDS = ("rect", "ellipse", "image", "paint")


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------
# (de)serializzazione generica dei dataclass
# --------------------------------------------------------------------------


def to_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        return {f.name: to_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, list):
        return [to_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    return obj


def _unwrap_optional(ftype: Any) -> Any:
    """``TextStyle | None`` -> ``TextStyle``; lascia intatto il resto."""
    if get_origin(ftype) in (Union, types.UnionType):
        args = [a for a in get_args(ftype) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return ftype


def from_dict(cls: type, data: Any) -> Any:
    """Costruisce un dataclass da dict ignorando le chiavi sconosciute.

    Tollerante in avanti: un documento salvato da una versione futura si apre
    comunque, perdendo solo i campi che non conosciamo.
    """
    if not is_dataclass(cls):
        return data
    if data is None:
        return cls()
    hints = get_type_hints(cls)
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        val = data[f.name]
        ftype = _unwrap_optional(hints[f.name])
        if val is not None and is_dataclass(ftype):
            val = from_dict(ftype, val)
        elif get_origin(ftype) is list and val is not None:
            (inner,) = get_args(ftype) or (Any,)
            if is_dataclass(inner):
                val = [from_dict(inner, v) for v in val]
        kwargs[f.name] = val
    return cls(**kwargs)


# --------------------------------------------------------------------------
# parti del documento
# --------------------------------------------------------------------------


@dataclass
class Asset:
    """Un file immagine usato dal documento. Non viene mai modificato.

    ``generated`` marca i file prodotti dall'editor (maschere IA, toppe di
    riempimento, livelli rasterizzati): stanno nella cartella degli asset del
    documento e si possono cancellare insieme a lui.
    """
    id: str
    path: str
    width: int = 0
    height: int = 0
    name: str = ""
    generated: bool = False


@dataclass
class Effect:
    type: str
    params: dict = field(default_factory=dict)
    enabled: bool = True


@dataclass
class Mask:
    """Maschera di livello: bianco = visibile, nero = nascosto.

    ``rect`` ed ``ellipse`` hanno ``box`` in pixel del *documento* (una zona
    dell'immagine finale). ``image`` e' un asset in scala di grigi allineato ai
    pixel *del contenuto* del livello: segue spostamenti, scala e rotazione,
    come una maschera di Photoshop collegata. E' la forma dello scontorno IA.
    ``paint`` parte tutta da ``base`` (1 = tutto visibile, 0 = tutto nascosto).

    Sopra la forma, qualunque sia, si dipinge: ``strokes`` in pixel del
    documento, bianco rivela e nero nasconde (conta la luminosita' del colore).
    Cosi' si rifinisce a pennello anche uno scontorno IA.
    """
    kind: str = "rect"
    box: list[float] | None = None
    asset: str | None = None
    invert: bool = False
    feather: float = 0.0
    density: float = 1.0
    base: float = 1.0
    strokes: list["Stroke"] = field(default_factory=list)


@dataclass
class TextStyle:
    font: str = "sans"
    size: float = 64.0
    color: str = "#ffffff"
    bold: bool = False
    italic: bool = False
    align: str = "left"
    line_spacing: float = 1.2
    stroke_width: float = 0.0
    stroke_color: str = "#000000"
    # 0 = nessun a capo automatico; altrimenti larghezza massima in pixel
    max_width: float = 0.0


@dataclass
class Shape:
    kind: str = "rect"
    w: float = 200.0
    h: float = 200.0
    fill: str | None = "#ffffff"
    stroke: str | None = None
    stroke_width: float = 0.0
    radius: float = 0.0
    # solo polygon/line: punti relativi all'angolo del livello
    points: list[list[float]] | None = None


@dataclass
class Fill:
    kind: str = "solid"
    color: str = "#000000"
    color2: str = "#ffffff"
    angle: float = 90.0
    # radiale: centro in frazioni del documento
    cx: float = 0.5
    cy: float = 0.5


@dataclass
class Stroke:
    """Una pennellata: polilinea in pixel del documento.

    Le pennellate restano vettoriali nel documento e si rasterizzano al render,
    quindi una pennellata sbagliata si toglie senza toccare le altre.
    """
    points: list[list[float]] = field(default_factory=list)
    color: str = "#000000"
    size: float = 12.0
    hardness: float = 0.8
    opacity: float = 1.0
    erase: bool = False
    # timbro clone: i pixel arrivano da (punto + clone), presi da cio' che sta
    # sotto il livello; con heal la luce e il colore si adattano alla zona
    # di arrivo (pennello correttivo)
    clone: list[float] | None = None
    heal: bool = False


@dataclass
class Layer:
    id: str
    type: str = "image"
    name: str = ""
    visible: bool = True
    locked: bool = False
    opacity: float = 1.0
    blend: str = "normal"
    # trasformazione del contenuto
    x: float = 0.0
    y: float = 0.0
    scale: float = 1.0
    rotation: float = 0.0
    flip_h: bool = False
    flip_v: bool = False
    # contenuto, secondo il tipo
    asset: str | None = None
    crop: list[float] | None = None
    text: str | None = None
    style: TextStyle | None = None
    shape: Shape | None = None
    fill: Fill | None = None
    strokes: list[Stroke] = field(default_factory=list)
    # filtri e stili (su un livello di regolazione: applicati a cio' che sta sotto)
    effects: list[Effect] = field(default_factory=list)
    mask: Mask | None = None
    # maschera di ritaglio: il livello si vede solo dove c'e' quello sotto
    clip: bool = False
    # solo per i gruppi: i figli, dal basso verso l'alto
    children: list["Layer"] = field(default_factory=list)
    collapsed: bool = False


@dataclass
class Project:
    name: str = "untitled"
    width: int = 1920
    height: int = 1080
    background: str = "#ffffff"
    dpi: int = 72
    # dal basso verso l'alto: l'ultimo e' quello disegnato sopra a tutti
    layers: list[Layer] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    version: int = 1

    def to_dict(self) -> dict:
        return to_dict(self)

    @classmethod
    def from_obj(cls, data: dict) -> "Project":
        return from_dict(cls, data)

    # ---- ricerca --------------------------------------------------------
    def walk(self, layers: list[Layer] | None = None):
        """Tutti i livelli, anche dentro i gruppi (dal basso, prima il gruppo poi i figli)."""
        for ly in self.layers if layers is None else layers:
            yield ly
            if ly.children:
                yield from self.walk(ly.children)

    def layer(self, layer_id: str) -> Layer:
        for ly in self.walk():
            if ly.id == layer_id:
                return ly
        # anche per nome: l'agente spesso ricorda "Title" e non "L3fa9c1d2"
        for ly in self.walk():
            if ly.name and ly.name == layer_id:
                return ly
        raise KeyError(f"layer not found: {layer_id}")

    def container(self, layer_id: str) -> list[Layer]:
        """La lista che contiene il livello: ``layers`` o i figli di un gruppo."""
        target = self.layer(layer_id)
        if any(ly is target for ly in self.layers):
            return self.layers
        for ly in self.walk():
            if any(c is target for c in ly.children):
                return ly.children
        raise KeyError(f"layer not found: {layer_id}")

    def parent(self, layer_id: str) -> Layer | None:
        target = self.layer(layer_id)
        for ly in self.walk():
            if any(c is target for c in ly.children):
                return ly
        return None

    def index_of(self, layer_id: str) -> int:
        """Posizione dentro la sua lista (``container``)."""
        target = self.layer(layer_id)
        return next(i for i, ly in enumerate(self.container(layer_id)) if ly is target)

    def asset(self, asset_id: str) -> Asset:
        for a in self.assets:
            if a.id == asset_id:
                return a
        raise KeyError(f"asset not found: {asset_id}")
