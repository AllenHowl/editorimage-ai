"""Operazioni di editing sul documento + persistenza.

Questo modulo e' l'unica superficie di modifica: il server MCP, l'API della UI,
l'assistente in chat e la riga di comando chiamano gli stessi metodi, quindi UI
e agente non possono divergere. Ogni mutazione prende uno snapshot per
undo/redo e (se il documento ha un file) salva su disco.

Anche le operazioni IA passano di qui, e restano non distruttive: lo scontorno
diventa una maschera, la rimozione di un oggetto una toppa su un livello nuovo,
l'upscale un asset nuovo. L'originale non si tocca mai.
"""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import ai
from . import analyze
from . import effects as fx
from . import presets
from . import render
from .model import (
    BLEND_MODES,
    FILL_KINDS,
    IMAGE_EXTS,
    MASK_KINDS,
    SHAPE_KINDS,
    Asset,
    Effect,
    Fill,
    Layer,
    Mask,
    Project,
    Shape,
    Stroke,
    TextStyle,
    new_id,
)

MAX_HISTORY = 100

PRESETS = {
    "1080p": (1920, 1080),
    "4k": (3840, 2160),
    "square": (1080, 1080),
    "instagram_post": (1080, 1350),
    "story": (1080, 1920),
    "youtube_thumbnail": (1280, 720),
    "a4_300dpi": (2480, 3508),
    "a4_150dpi": (1240, 1754),
    "web_banner": (1500, 500),
    "og_image": (1200, 630),
}

ALIGN_H = ("left", "center", "right")
ALIGN_V = ("top", "middle", "bottom")


class EditError(ValueError):
    """Errore di editing con messaggio comprensibile all'utente."""


def _home() -> Path:
    base = Path(os.environ.get("PEDIT_HOME") or (Path.home() / ".pedit"))
    base.mkdir(parents=True, exist_ok=True)
    return base


class Store:
    def __init__(self, project: Project, path: str | None = None, autosave: bool = True):
        self.project = project
        self.path = str(Path(path).resolve()) if path else None
        self.autosave = autosave
        self._undo: list[dict] = []
        self._redo: list[dict] = []
        self._batch = 0
        self.version = 0
        # chiamata dopo ogni modifica conclusa: la UI si aggiorna anche quando a
        # modificare e' l'agente. Stesso Store, un solo scrittore.
        self.on_change = None
        # cartella degli asset per un documento non ancora salvato
        self._scratch: Path | None = None

    # ---- ciclo di vita -------------------------------------------------
    @classmethod
    def create(cls, name: str = "untitled", preset: str | None = None, width: int | None = None,
               height: int | None = None, background: str = "#ffffff", path: str | None = None,
               dpi: int = 72, from_image: str | None = None) -> "Store":
        if from_image:
            img = cls._probe(from_image)
            w, h = img
        elif preset:
            if preset not in PRESETS:
                raise EditError(f"unknown preset {preset!r}; available: {', '.join(PRESETS)}")
            w, h = PRESETS[preset]
        else:
            w, h = 1920, 1080
        w, h = int(width or w), int(height or h)
        if not (1 <= w <= 20000 and 1 <= h <= 20000):
            raise EditError("width and height must be between 1 and 20000 px")
        fx.parse_color(background)
        s = cls(Project(name=name, width=w, height=h, background=background, dpi=dpi), path)
        if from_image:
            s._autosave_off(lambda: s.add_image(path=from_image, x=0, y=0, fit="none", name=Path(from_image).stem))
            s._undo.clear()
        if path:
            s.save()
        return s

    def _autosave_off(self, fn):
        old, self.autosave = self.autosave, False
        try:
            return fn()
        finally:
            self.autosave = old

    @classmethod
    def open(cls, path: str) -> "Store":
        f = Path(path)
        if not f.exists():
            raise EditError(f"document not found: {path}")
        if f.suffix.lower() in IMAGE_EXTS:
            # aprire una foto = documento nuovo grande quanto lei, non salvato
            return cls.create(name=f.stem, from_image=str(f))
        data = json.loads(f.read_text(encoding="utf-8"))
        return cls(Project.from_obj(data), str(f.resolve()))

    def save(self, path: str | None = None) -> str:
        """Salvataggio atomico: ``os.replace``, mai un file a meta'."""
        target = Path(path or self.path or "")
        if not str(target):
            raise EditError("no save path: pass path=")
        if target.suffix.lower() != ".json":
            raise EditError("a pedit document is a .json file; to write pixels use export_image")
        target.parent.mkdir(parents=True, exist_ok=True)
        vecchia_base = self.base_dir
        self.path = str(target.resolve())
        self._riloca_asset(vecchia_base)
        blob = json.dumps(self.project.to_dict(), indent=2, ensure_ascii=False)
        last: OSError | None = None
        for attempt in range(10):
            tmp = target.with_name(f"~{uuid.uuid4().hex[:8]}_{target.name}")
            try:
                tmp.write_text(blob, encoding="utf-8")
                os.replace(tmp, target)
                break
            except OSError as exc:
                tmp.unlink(missing_ok=True)
                last = exc
                time.sleep(0.02 * (attempt + 1))
        else:
            raise EditError(f"saving to {target} failed: {last}")
        return self.path

    def _riloca_asset(self, vecchia_base: str | None) -> None:
        """Percorsi relativi restano validi quando il documento cambia cartella."""
        for a in self.project.assets:
            p = render.resolve(a.path, vecchia_base)
            a.path = self._percorso_asset(p)

    def _percorso_asset(self, p: Path) -> str:
        p = p.resolve()
        if self.base_dir:
            try:
                return p.relative_to(self.base_dir).as_posix()
            except ValueError:
                pass
        return str(p)

    @property
    def base_dir(self) -> str | None:
        return str(Path(self.path).parent) if self.path else None

    def assets_dir(self) -> Path:
        """Dove finiscono maschere IA, toppe, livelli rasterizzati."""
        if self.path:
            d = Path(self.path).with_name(Path(self.path).stem + "_assets")
        else:
            if self._scratch is None:
                self._scratch = _home() / "scratch" / uuid.uuid4().hex[:12]
            d = self._scratch
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---- undo/redo -----------------------------------------------------
    def _touch(self) -> None:
        if self._batch:
            return
        self._undo.append(self.project.to_dict())
        if len(self._undo) > MAX_HISTORY:
            self._undo.pop(0)
        self._redo.clear()

    def _done(self) -> None:
        if self._batch:
            return
        self.version += 1
        if self.autosave and self.path:
            self.save()
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001 - un osservatore rotto non annulla la modifica
                pass

    @contextmanager
    def batch(self):
        """Piu' operazioni come una: un undo, un salvataggio, tutto o niente."""
        if self._batch:
            self._batch += 1
            try:
                yield self
            finally:
                self._batch -= 1
            return
        self._undo.append(self.project.to_dict())
        if len(self._undo) > MAX_HISTORY:
            self._undo.pop(0)
        self._redo.clear()
        self._batch = 1
        try:
            yield self
        except Exception:
            self.project = Project.from_obj(self._undo.pop())
            self.version += 1
            raise
        finally:
            self._batch = 0
        self._done()

    def undo(self) -> dict:
        if not self._undo:
            raise EditError("nothing to undo")
        self._redo.append(self.project.to_dict())
        self.project = Project.from_obj(self._undo.pop())
        self._done()
        return {"undo": len(self._undo), "redo": len(self._redo)}

    def redo(self) -> dict:
        if not self._redo:
            raise EditError("nothing to redo")
        self._undo.append(self.project.to_dict())
        self.project = Project.from_obj(self._redo.pop())
        self._done()
        return {"undo": len(self._undo), "redo": len(self._redo)}

    @contextmanager
    def _edit(self):
        """Una modifica semplice: snapshot prima, salvataggio dopo, e se fallisce si torna com'era."""
        prima = self.project.to_dict() if not self._batch else None
        self._touch()
        try:
            yield
        except Exception:
            if prima is not None:
                self.project = Project.from_obj(prima)
                self._undo.pop()
            raise
        self._done()

    # ---- lettura -------------------------------------------------------
    def summary(self, detail: str = "normal") -> dict:
        p = self.project
        out = {"name": p.name, "width": p.width, "height": p.height, "background": p.background,
               "dpi": p.dpi, "path": self.path, "undo": len(self._undo), "redo": len(self._redo)}
        out["layers"] = [self.layer_view(ly, detail) for ly in reversed(p.layers)]
        if detail == "full":
            out["assets"] = [vars(a) | {} for a in p.assets]
        else:
            out["assets"] = [{"id": a.id, "name": a.name, "width": a.width, "height": a.height,
                              "generated": a.generated} for a in p.assets]
        return out

    def layer_view(self, ly: Layer, detail: str = "normal") -> dict:
        """Vista compatta di un livello: quello che serve per decidere la prossima mossa."""
        v: dict[str, Any] = {"id": ly.id, "name": ly.name, "type": ly.type}
        for k, default in (("visible", True), ("locked", False), ("opacity", 1.0), ("blend", "normal")):
            if getattr(ly, k) != default or detail == "full":
                v[k] = getattr(ly, k)
        if ly.type in ("image", "text", "shape"):
            try:
                w, h = render.content_size(ly, self.project, self.base_dir)
            except (KeyError, FileNotFoundError, OSError):
                w, h = 0, 0
            v["box"] = [round(ly.x, 1), round(ly.y, 1), round(w, 1), round(h, 1)]
            if ly.scale != 1:
                v["scale"] = round(ly.scale, 4)
            if ly.rotation:
                v["rotation"] = ly.rotation
            if ly.flip_h or ly.flip_v:
                v["flip"] = [ly.flip_h, ly.flip_v]
        if ly.type == "image":
            v["asset"] = ly.asset
            if ly.crop:
                v["crop"] = ly.crop
        if ly.type == "text":
            v["text"] = ly.text
            st = ly.style
            v["style"] = vars(st) if detail == "full" else {"font": st.font, "size": st.size, "color": st.color}
        if ly.type == "shape":
            v["shape"] = vars(ly.shape)
        if ly.type == "fill":
            v["fill"] = vars(ly.fill)
        if ly.type == "paint":
            v["strokes"] = len(ly.strokes)
        if ly.effects:
            v["effects"] = [{"type": e.type, **({"params": e.params} if detail == "full" else {}),
                             **({} if e.enabled else {"enabled": False})} for e in ly.effects]
        if ly.mask:
            v["mask"] = {k: x for k, x in vars(ly.mask).items() if x not in (None, False, 0.0) or k == "kind"}
        return v

    # ---- helper --------------------------------------------------------
    def _layer(self, layer: str, edit: bool = True) -> Layer:
        if not layer:
            raise EditError("which layer? pass layer=<id or name>")
        try:
            ly = self.project.layer(layer)
        except KeyError as exc:
            ids = ", ".join(f"{x.id} ({x.name})" for x in self.project.layers) or "none"
            raise EditError(f"layer not found: {layer}; layers: {ids}") from exc
        if edit and ly.locked:
            raise EditError(f"layer {ly.name or ly.id} is locked: set_layer(locked=false) first")
        return ly

    def _insert(self, ly: Layer, index: int | None = None, above: str | None = None) -> None:
        if above:
            i = self.project.index_of(self._layer(above, edit=False).id) + 1
        elif index is None:
            i = len(self.project.layers)
        else:
            i = max(0, min(int(index), len(self.project.layers)))
        self.project.layers.insert(i, ly)

    def _nome(self, base: str) -> str:
        nomi = {ly.name for ly in self.project.layers}
        if base not in nomi:
            return base
        n = 2
        while f"{base} {n}" in nomi:
            n += 1
        return f"{base} {n}"

    @staticmethod
    def _probe(path: str) -> tuple[int, int]:
        p = Path(path)
        if not p.is_file():
            raise EditError(f"image not found: {path}")
        try:
            with Image.open(p) as im:
                from PIL import ImageOps
                im2 = ImageOps.exif_transpose(im)
                return im2.size
        except OSError as exc:
            raise EditError(f"not a readable image: {path} ({exc})") from exc

    def _new_asset(self, img: Image.Image, name: str, prefix: str = "gen") -> Asset:
        d = self.assets_dir()
        f = d / f"{prefix}_{uuid.uuid4().hex[:8]}.png"
        img.save(f, "PNG")
        a = Asset(id=new_id("a"), path=self._percorso_asset(f), width=img.width, height=img.height,
                  name=name, generated=True)
        self.project.assets.append(a)
        return a

    def _asset_img(self, asset_id: str) -> Image.Image:
        a = self.project.asset(asset_id)
        return render.load_image(render.resolve(a.path, self.base_dir))

    @staticmethod
    def _check_box(box: Any, what: str = "box") -> list[float]:
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            raise EditError(f"{what} must be [x, y, width, height] in document pixels")
        x, y, w, h = (float(v) for v in box)
        if w <= 0 or h <= 0:
            raise EditError(f"{what}: width and height must be positive")
        return [x, y, w, h]

    def _clip_box(self, box: list[float]) -> tuple[int, int, int, int]:
        x, y, w, h = box
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(self.project.width, int(round(x + w))), min(self.project.height, int(round(y + h)))
        if x1 <= x0 or y1 <= y0:
            raise EditError("the box is outside the canvas")
        return x0, y0, x1, y1

    # ---- documento -----------------------------------------------------
    def set_document(self, name: str | None = None, background: str | None = None,
                     dpi: int | None = None) -> dict:
        with self._edit():
            if name is not None:
                self.project.name = name
            if background is not None:
                fx.parse_color(background)
                self.project.background = background
            if dpi is not None:
                self.project.dpi = int(dpi)
        return {"name": self.project.name, "background": self.project.background, "dpi": self.project.dpi}

    def import_images(self, paths: list[str]) -> list[dict]:
        """Registra file come asset (non li copia, non crea livelli)."""
        if isinstance(paths, str):
            paths = [paths]
        with self._edit():
            nuovi = [self.project.asset(self._import_one(p)) for p in paths]
        return [{"id": a.id, "name": a.name, "width": a.width, "height": a.height} for a in nuovi]

    def remove_asset(self, asset: str) -> dict:
        a = self.project.asset(asset)
        usato = [ly.id for ly in self.project.layers
                 if ly.asset == a.id or (ly.mask and ly.mask.asset == a.id)]
        if usato:
            raise EditError(f"asset {asset} is used by layers {usato}")
        with self._edit():
            self.project.assets.remove(a)
        return {"removed": asset}

    # ---- livelli: creazione --------------------------------------------
    def add_image(self, asset: str | None = None, path: str | None = None, x: float | None = None,
                  y: float | None = None, scale: float | None = None, fit: str = "contain",
                  name: str | None = None, index: int | None = None, above: str | None = None) -> dict:
        """Livello immagine. ``fit``: contain (dentro la tela), cover (la riempie), width, none."""
        if fit not in ("contain", "cover", "width", "height", "none"):
            raise EditError("fit must be contain, cover, width, height or none")
        with self._edit():
            if asset is None:
                if not path:
                    raise EditError("pass asset=<id> or path=<file>")
                asset = self._import_one(path)
            a = self.project.asset(asset)
            W, H = self.project.width, self.project.height
            if scale is None:
                if fit == "none":
                    scale = 1.0
                elif fit == "contain":
                    scale = min(1.0, W / a.width, H / a.height)
                elif fit == "cover":
                    scale = max(W / a.width, H / a.height)
                elif fit == "width":
                    scale = W / a.width
                else:
                    scale = H / a.height
            if scale <= 0:
                raise EditError("scale must be positive")
            w, h = a.width * scale, a.height * scale
            ly = Layer(id=new_id("L"), type="image", name=self._nome(name or a.name or "Image"), asset=a.id,
                       scale=float(scale),
                       x=float((W - w) / 2 if x is None else x), y=float((H - h) / 2 if y is None else y))
            self._insert(ly, index, above)
        return self.layer_view(ly)

    def _import_one(self, path: str) -> str:
        w, h = self._probe(path)
        p = Path(path).resolve()
        for a in self.project.assets:
            if render.resolve(a.path, self.base_dir).resolve() == p:
                return a.id
        a = Asset(id=new_id("a"), path=self._percorso_asset(p), width=w, height=h, name=p.stem)
        self.project.assets.append(a)
        return a.id

    def add_text(self, text: str, x: float | None = None, y: float | None = None, size: float = 64,
                 color: str = "#ffffff", font: str = "sans", bold: bool = False, italic: bool = False,
                 align: str = "left", line_spacing: float = 1.2, stroke_width: float = 0,
                 stroke_color: str = "#000000", max_width: float = 0, name: str | None = None,
                 index: int | None = None, above: str | None = None) -> dict:
        if not text:
            raise EditError("text is empty")
        if align not in ("left", "center", "right"):
            raise EditError("align must be left, center or right")
        if size <= 0:
            raise EditError("size must be positive")
        fx.parse_color(color)
        fx.parse_color(stroke_color)
        with self._edit():
            st = TextStyle(font=font, size=float(size), color=color, bold=bold, italic=italic, align=align,
                           line_spacing=float(line_spacing), stroke_width=float(stroke_width),
                           stroke_color=stroke_color, max_width=float(max_width))
            ly = Layer(id=new_id("L"), type="text", name=self._nome(name or text.split("\n")[0][:24]),
                       text=text, style=st)
            w, h = render.text_size(ly)
            ly.x = float((self.project.width - w) / 2 if x is None else x)
            ly.y = float((self.project.height - h) / 2 if y is None else y)
            self._insert(ly, index, above)
        return self.layer_view(ly)

    def add_shape(self, kind: str = "rect", x: float = 0, y: float = 0, w: float = 200, h: float = 200,
                  fill: str | None = "#ffffff", stroke: str | None = None, stroke_width: float = 0,
                  radius: float = 0, points: list | None = None, name: str | None = None,
                  index: int | None = None, above: str | None = None) -> dict:
        if kind not in SHAPE_KINDS:
            raise EditError(f"kind must be one of {', '.join(SHAPE_KINDS)}")
        if w <= 0 or h <= 0:
            raise EditError("w and h must be positive")
        for c in (fill, stroke):
            if c:
                fx.parse_color(c)
        with self._edit():
            sh = Shape(kind=kind, w=float(w), h=float(h), fill=fill, stroke=stroke,
                       stroke_width=float(stroke_width), radius=float(radius),
                       points=[[float(a), float(b)] for a, b in points] if points else None)
            ly = Layer(id=new_id("L"), type="shape", name=self._nome(name or kind.capitalize()),
                       x=float(x), y=float(y), shape=sh)
            self._insert(ly, index, above)
        return self.layer_view(ly)

    def add_fill(self, kind: str = "solid", color: str = "#000000", color2: str = "#ffffff",
                 angle: float = 90, cx: float = 0.5, cy: float = 0.5, name: str | None = None,
                 index: int | None = None, above: str | None = None) -> dict:
        if kind not in FILL_KINDS:
            raise EditError(f"kind must be one of {', '.join(FILL_KINDS)}")
        fx.parse_color(color)
        fx.parse_color(color2)
        with self._edit():
            ly = Layer(id=new_id("L"), type="fill", name=self._nome(name or ("Gradient" if kind != "solid" else "Fill")),
                       fill=Fill(kind=kind, color=color, color2=color2, angle=float(angle), cx=float(cx), cy=float(cy)))
            self._insert(ly, index, above)
        return self.layer_view(ly)

    def add_adjustment(self, type: str | None = None, params: dict | None = None,
                       effects: list[dict] | None = None, name: str | None = None,
                       index: int | None = None, above: str | None = None) -> dict:
        """Livello di regolazione: i suoi filtri agiscono su tutto cio' che sta sotto."""
        chain = list(effects or [])
        if type:
            chain.insert(0, {"type": type, "params": params or {}})
        if not chain:
            raise EditError("pass type=<effect> (with params) or effects=[{type, params}, ...]")
        effs = []
        for e in chain:
            if fx.spec(e["type"])["kind"] != "filter":
                raise EditError(f"{e['type']} is a layer style: it cannot go on an adjustment layer")
            effs.append(Effect(type=e["type"], params=fx.validate(e["type"], e.get("params"))))
        with self._edit():
            ly = Layer(id=new_id("L"), type="adjustment", name=self._nome(name or chain[0]["type"].replace("_", " ").capitalize()),
                       effects=effs)
            self._insert(ly, index, above)
        return self.layer_view(ly)

    def add_paint_layer(self, name: str | None = None, index: int | None = None,
                        above: str | None = None) -> dict:
        with self._edit():
            ly = Layer(id=new_id("L"), type="paint", name=self._nome(name or "Paint"))
            self._insert(ly, index, above)
        return self.layer_view(ly)

    def paint_stroke(self, points: list, layer: str | None = None, color: str = "#000000",
                     size: float = 12, hardness: float = 0.8, opacity: float = 1.0,
                     erase: bool = False) -> dict:
        """Una pennellata (polilinea in pixel del documento) su un livello di pittura."""
        if not points or not all(isinstance(p, (list, tuple)) and len(p) == 2 for p in points):
            raise EditError("points must be a list of [x, y]")
        fx.parse_color(color)
        if not 0 <= hardness <= 1 or not 0 <= opacity <= 1 or size <= 0:
            raise EditError("hardness and opacity in 0..1, size > 0")
        with self._edit():
            if layer is None:
                top = self.project.layers[-1] if self.project.layers else None
                if top is not None and top.type == "paint" and not top.locked:
                    ly = top
                else:
                    ly = Layer(id=new_id("L"), type="paint", name=self._nome("Paint"))
                    self.project.layers.append(ly)
            else:
                ly = self._layer(layer)
                if ly.type != "paint":
                    raise EditError(f"{ly.name or ly.id} is a {ly.type} layer: paint goes on a paint layer "
                                    "(add_paint_layer, or omit layer=)")
            ly.strokes.append(Stroke(points=[[float(a), float(b)] for a, b in points], color=color,
                                     size=float(size), hardness=float(hardness), opacity=float(opacity),
                                     erase=bool(erase)))
        return {"layer": ly.id, "strokes": len(ly.strokes)}

    def remove_stroke(self, layer: str, index: int = -1) -> dict:
        ly = self._layer(layer)
        if ly.type != "paint" or not ly.strokes:
            raise EditError("no strokes to remove")
        with self._edit():
            ly.strokes.pop(index)
        return {"layer": ly.id, "strokes": len(ly.strokes)}

    # ---- livelli: proprieta' -------------------------------------------
    def set_layer(self, layer: str, name: str | None = None, visible: bool | None = None,
                  locked: bool | None = None, opacity: float | None = None, blend: str | None = None) -> dict:
        ly = self._layer(layer, edit=False)
        if ly.locked and locked is not False and any(v is not None for v in (name, opacity, blend)):
            raise EditError(f"layer {ly.name or ly.id} is locked")
        if blend is not None and blend not in BLEND_MODES:
            raise EditError(f"blend must be one of {', '.join(BLEND_MODES)}")
        if opacity is not None and not 0 <= opacity <= 1:
            raise EditError("opacity must be in 0..1")
        with self._edit():
            if name is not None:
                ly.name = name
            if visible is not None:
                ly.visible = bool(visible)
            if locked is not None:
                ly.locked = bool(locked)
            if opacity is not None:
                ly.opacity = float(opacity)
            if blend is not None:
                ly.blend = blend
        return self.layer_view(ly)

    def set_transform(self, layer: str, x: float | None = None, y: float | None = None,
                      scale: float | None = None, rotation: float | None = None,
                      flip_h: bool | None = None, flip_v: bool | None = None,
                      crop: list | None = None, width: float | None = None, height: float | None = None) -> dict:
        """Posizione, scala, rotazione (gradi, orario), ribaltamenti, ritaglio della sorgente.

        ``width``/``height`` sono un modo comodo di dare la scala: la dimensione
        che si vuole sul documento, mantenendo le proporzioni.
        """
        ly = self._layer(layer)
        if ly.type in ("fill", "adjustment", "paint"):
            raise EditError(f"a {ly.type} layer covers the whole canvas and has no transform")
        with self._edit():
            if crop is not None:
                if ly.type != "image":
                    raise EditError("crop applies to image layers")
                if crop:
                    a = self.project.asset(ly.asset)
                    cx, cy, cw, ch = self._check_box(crop, "crop")
                    if cx < 0 or cy < 0 or cx + cw > a.width + 0.5 or cy + ch > a.height + 0.5:
                        raise EditError(f"crop outside the source image ({a.width}x{a.height})")
                    ly.crop = [cx, cy, cw, ch]
                else:
                    ly.crop = None
            if width is not None or height is not None:
                ly_scale_1 = copy.copy(ly)
                ly_scale_1.scale = 1.0
                w1, h1 = render.content_size(ly_scale_1, self.project, self.base_dir)
                scale = (width / w1) if width is not None else (height / h1)
            if scale is not None:
                if scale <= 0:
                    raise EditError("scale must be positive")
                ly.scale = float(scale)
            if x is not None:
                ly.x = float(x)
            if y is not None:
                ly.y = float(y)
            if rotation is not None:
                ly.rotation = float(rotation) % 360
            if flip_h is not None:
                ly.flip_h = bool(flip_h)
            if flip_v is not None:
                ly.flip_v = bool(flip_v)
        return self.layer_view(ly)

    def align_layer(self, layer: str, h: str | None = "center", v: str | None = "middle",
                    margin: float = 0) -> dict:
        """Allinea un livello alla tela: h left/center/right, v top/middle/bottom, margine in px."""
        ly = self._layer(layer)
        if h is not None and h not in ALIGN_H:
            raise EditError(f"h must be one of {', '.join(ALIGN_H)}")
        if v is not None and v not in ALIGN_V:
            raise EditError(f"v must be one of {', '.join(ALIGN_V)}")
        w, hh = render.content_size(ly, self.project, self.base_dir)
        W, H = self.project.width, self.project.height
        with self._edit():
            if h == "left":
                ly.x = float(margin)
            elif h == "center":
                ly.x = (W - w) / 2
            elif h == "right":
                ly.x = W - w - margin
            if v == "top":
                ly.y = float(margin)
            elif v == "middle":
                ly.y = (H - hh) / 2
            elif v == "bottom":
                ly.y = H - hh - margin
        return self.layer_view(ly)

    def set_text(self, layer: str, text: str | None = None, **style: Any) -> dict:
        ly = self._layer(layer)
        if ly.type != "text":
            raise EditError(f"{ly.name or ly.id} is not a text layer")
        validi = set(vars(TextStyle()))
        extra = set(k for k, v in style.items() if v is not None) - validi
        if extra:
            raise EditError(f"unknown text properties {sorted(extra)}; accepted: {sorted(validi)}")
        with self._edit():
            if text is not None:
                if not text:
                    raise EditError("text is empty")
                ly.text = text
            for k, v in style.items():
                if v is None:
                    continue
                if k in ("color", "stroke_color"):
                    fx.parse_color(v)
                if k == "align" and v not in ("left", "center", "right"):
                    raise EditError("align must be left, center or right")
                setattr(ly.style, k, type(getattr(ly.style, k))(v))
        return self.layer_view(ly)

    def set_shape(self, layer: str, **props: Any) -> dict:
        ly = self._layer(layer)
        if ly.type != "shape":
            raise EditError(f"{ly.name or ly.id} is not a shape layer")
        validi = set(vars(Shape()))
        extra = set(k for k, v in props.items() if v is not None) - validi
        if extra:
            raise EditError(f"unknown shape properties {sorted(extra)}; accepted: {sorted(validi)}")
        with self._edit():
            for k, v in props.items():
                if v is None:
                    continue
                if k == "kind" and v not in SHAPE_KINDS:
                    raise EditError(f"kind must be one of {', '.join(SHAPE_KINDS)}")
                if k in ("fill", "stroke") and v:
                    fx.parse_color(v)
                if k in ("fill", "stroke") and v in ("", "none"):
                    v = None
                setattr(ly.shape, k, v)
        return self.layer_view(ly)

    def set_fill(self, layer: str, **props: Any) -> dict:
        ly = self._layer(layer)
        if ly.type != "fill":
            raise EditError(f"{ly.name or ly.id} is not a fill layer")
        with self._edit():
            for k, v in props.items():
                if v is None:
                    continue
                if k not in vars(Fill()):
                    raise EditError(f"unknown fill property {k}")
                if k == "kind" and v not in FILL_KINDS:
                    raise EditError(f"kind must be one of {', '.join(FILL_KINDS)}")
                if k in ("color", "color2"):
                    fx.parse_color(v)
                setattr(ly.fill, k, v)
        return self.layer_view(ly)

    # ---- livelli: ordine ------------------------------------------------
    def move_layer(self, layer: str, index: int | None = None, to: str | None = None) -> dict:
        """``index`` 0 = in fondo; oppure ``to`` = up, down, top, bottom."""
        ly = self._layer(layer, edit=False)
        layers = self.project.layers
        i = self.project.index_of(ly.id)
        if to is not None:
            dest = {"up": i + 1, "down": i - 1, "top": len(layers) - 1, "bottom": 0}.get(to)
            if dest is None:
                raise EditError("to must be up, down, top or bottom")
        elif index is not None:
            dest = int(index)
        else:
            raise EditError("pass index= or to=")
        dest = max(0, min(dest, len(layers) - 1))
        with self._edit():
            layers.insert(dest, layers.pop(i))
        return {"order": [x.id for x in reversed(layers)]}

    def duplicate_layer(self, layer: str, name: str | None = None) -> dict:
        ly = self._layer(layer, edit=False)
        with self._edit():
            nuovo = copy.deepcopy(ly)
            nuovo.id = new_id("L")
            nuovo.name = self._nome(name or f"{ly.name} copy")
            nuovo.locked = False
            self.project.layers.insert(self.project.index_of(ly.id) + 1, nuovo)
        return self.layer_view(nuovo)

    def remove_layer(self, layer: str) -> dict:
        ly = self._layer(layer)
        with self._edit():
            self.project.layers.remove(ly)
        return {"removed": ly.id}

    # ---- effetti ---------------------------------------------------------
    def add_effect(self, type: str, layer: str, params: dict | None = None, index: int | None = None) -> dict:
        ly = self._layer(layer)
        sp = fx.spec(type)
        if ly.type == "adjustment" and sp["kind"] != "filter":
            raise EditError(f"{type} is a layer style: it cannot go on an adjustment layer")
        p = fx.validate(type, params)
        with self._edit():
            e = Effect(type=type, params=p)
            if index is None:
                ly.effects.append(e)
            else:
                ly.effects.insert(int(index), e)
        return {"layer": ly.id, "effects": [x.type for x in ly.effects]}

    def update_effect(self, layer: str, index: int, params: dict | None = None,
                      enabled: bool | None = None) -> dict:
        ly = self._layer(layer)
        try:
            e = ly.effects[int(index)]
        except IndexError as exc:
            raise EditError(f"effect index {index} out of range: layer has {len(ly.effects)}") from exc
        upd = fx.validate(e.type, params, partial=True) if params else {}
        with self._edit():
            e.params.update(upd)
            if enabled is not None:
                e.enabled = bool(enabled)
        return {"layer": ly.id, "index": int(index), "type": e.type, "params": e.params, "enabled": e.enabled}

    def remove_effect(self, layer: str, index: int) -> dict:
        ly = self._layer(layer)
        if not -len(ly.effects) <= int(index) < len(ly.effects):
            raise EditError(f"effect index {index} out of range")
        with self._edit():
            ly.effects.pop(int(index))
            if ly.type == "adjustment" and not ly.effects:
                self.project.layers.remove(ly)
        return {"layer": ly.id, "effects": [x.type for x in ly.effects]}

    def move_effect(self, layer: str, index: int, to: int) -> dict:
        ly = self._layer(layer)
        with self._edit():
            e = ly.effects.pop(int(index))
            ly.effects.insert(int(to), e)
        return {"layer": ly.id, "effects": [x.type for x in ly.effects]}

    def apply_look(self, name: str, layer: str | None = None) -> dict:
        """Un look della libreria: come regolazione in cima, o come effetti su un livello."""
        if name not in presets.LOOKS:
            raise EditError(f"unknown look {name!r}; available: {', '.join(presets.LOOKS)}")
        chain = presets.LOOKS[name]["effects"]
        if layer is None:
            return self.add_adjustment(effects=chain, name=name.replace("_", " ").title())
        ly = self._layer(layer)
        with self._edit():
            for e in chain:
                ly.effects.append(Effect(type=e["type"], params=fx.validate(e["type"], e["params"])))
        return {"layer": ly.id, "effects": [x.type for x in ly.effects]}

    # ---- maschere --------------------------------------------------------
    def set_mask(self, layer: str, kind: str = "rect", box: list | None = None, asset: str | None = None,
                 path: str | None = None, invert: bool = False, feather: float = 0.0,
                 density: float = 1.0) -> dict:
        ly = self._layer(layer)
        if kind not in MASK_KINDS:
            raise EditError(f"kind must be one of {', '.join(MASK_KINDS)}")
        if not 0 <= density <= 1 or feather < 0:
            raise EditError("density in 0..1, feather >= 0")
        with self._edit():
            if kind == "image":
                if path:
                    asset = self._import_one(path)
                if not asset:
                    raise EditError("an image mask wants asset= or path=")
                self.project.asset(asset)
                ly.mask = Mask(kind="image", asset=asset, invert=bool(invert), feather=float(feather),
                               density=float(density))
            else:
                ly.mask = Mask(kind=kind, box=self._check_box(box) if box is not None else
                               [0.0, 0.0, float(self.project.width), float(self.project.height)],
                               invert=bool(invert), feather=float(feather), density=float(density))
        return self.layer_view(ly)

    def update_mask(self, layer: str, invert: bool | None = None, feather: float | None = None,
                    density: float | None = None, box: list | None = None) -> dict:
        ly = self._layer(layer)
        if ly.mask is None:
            raise EditError(f"{ly.name or ly.id} has no mask")
        with self._edit():
            if invert is not None:
                ly.mask.invert = bool(invert)
            if feather is not None:
                ly.mask.feather = max(0.0, float(feather))
            if density is not None:
                ly.mask.density = float(min(1, max(0, density)))
            if box is not None:
                if ly.mask.kind == "image":
                    raise EditError("an image mask has no box")
                ly.mask.box = self._check_box(box)
        return self.layer_view(ly)

    def remove_mask(self, layer: str) -> dict:
        ly = self._layer(layer)
        with self._edit():
            ly.mask = None
        return self.layer_view(ly)

    # ---- tela ------------------------------------------------------------
    def _sposta_tutto(self, dx: float, dy: float) -> None:
        for ly in self.project.layers:
            if ly.type in ("image", "text", "shape"):
                ly.x += dx
                ly.y += dy
            for st in ly.strokes:
                st.points = [[px + dx, py + dy] for px, py in st.points]
            if ly.mask and ly.mask.box:
                b = ly.mask.box
                ly.mask.box = [b[0] + dx, b[1] + dy, b[2], b[3]]

    def crop_canvas(self, x: float, y: float, width: float, height: float) -> dict:
        """Ritaglia la tela: i livelli restano interi (il ritaglio si disfa senza perdite)."""
        x, y, w, h = self._check_box([x, y, width, height])
        with self._edit():
            self._sposta_tutto(-x, -y)
            self.project.width, self.project.height = int(round(w)), int(round(h))
        return {"width": self.project.width, "height": self.project.height}

    def resize_canvas(self, width: int, height: int, anchor: str = "center") -> dict:
        """Cambia la tela senza scalare il contenuto. ``anchor``: center, top_left, top, ..."""
        ax = {"left": 0.0, "center": 0.5, "right": 1.0}
        ay = {"top": 0.0, "middle": 0.5, "center": 0.5, "bottom": 1.0}
        parti = anchor.split("_")
        if anchor == "center":
            fx_, fy_ = 0.5, 0.5
        elif len(parti) == 2 and parti[0] in ay and parti[1] in ax:
            fy_, fx_ = ay[parti[0]], ax[parti[1]]
        elif anchor in ax:
            fx_, fy_ = ax[anchor], 0.5
        elif anchor in ay:
            fx_, fy_ = 0.5, ay[anchor]
        else:
            raise EditError("anchor: center, top, bottom, left, right, top_left, top_right, bottom_left, bottom_right")
        if not (1 <= width <= 20000 and 1 <= height <= 20000):
            raise EditError("width and height must be between 1 and 20000")
        with self._edit():
            dx = (width - self.project.width) * fx_
            dy = (height - self.project.height) * fy_
            self._sposta_tutto(dx, dy)
            self.project.width, self.project.height = int(width), int(height)
        return {"width": self.project.width, "height": self.project.height}

    def resize_image(self, width: int | None = None, height: int | None = None) -> dict:
        """Scala tutto il documento (tela e contenuto). Con un solo lato, proporzioni mantenute."""
        W, H = self.project.width, self.project.height
        if width is None and height is None:
            raise EditError("pass width or height")
        kx = (width / W) if width else (height / H)
        ky = (height / H) if height else kx
        if width and height and abs(kx - ky) > 1e-3:
            raise EditError("resize_image keeps proportions: pass only one side, or use resize_canvas")
        k = kx
        with self._edit():
            for ly in self.project.layers:
                if ly.type in ("image", "text", "shape"):
                    ly.x *= k
                    ly.y *= k
                    ly.scale *= k
                for st in ly.strokes:
                    st.points = [[px * k, py * k] for px, py in st.points]
                    st.size *= k
                if ly.mask:
                    if ly.mask.box:
                        ly.mask.box = [v * k for v in ly.mask.box]
                    ly.mask.feather *= k
                for e in ly.effects:
                    for pk, ps in fx.spec(e.type)["params"].items():
                        if ps.get("px") and pk in e.params:
                            e.params[pk] = e.params[pk] * k
            self.project.width = max(1, int(round(W * k)))
            self.project.height = max(1, int(round(H * k)))
        return {"width": self.project.width, "height": self.project.height}

    # ---- rasterizzare ------------------------------------------------------
    def rasterize_layer(self, layer: str) -> dict:
        """Testo, forma, pittura o effetti diventano pixel: un livello immagine con un asset nuovo."""
        ly = self._layer(layer)
        if ly.type == "adjustment":
            raise EditError("an adjustment layer has no pixels of its own: use merge_down on it, or flatten")
        img = render.layer_image(self.project, ly.id, self.base_dir)
        bbox = img.getbbox()
        if bbox is None:
            raise EditError(f"{ly.name or ly.id} is empty")
        with self._edit():
            a = self._new_asset(img.crop(bbox), f"{ly.name} (raster)", "raster")
            nuovo = Layer(id=ly.id, type="image", name=ly.name, visible=ly.visible, opacity=ly.opacity,
                          blend=ly.blend, x=float(bbox[0]), y=float(bbox[1]), asset=a.id)
            self.project.layers[self.project.index_of(ly.id)] = nuovo
        return self.layer_view(nuovo)

    def merge_down(self, layer: str) -> dict:
        """Fonde il livello con quello sotto in un livello immagine."""
        i = self.project.index_of(self._layer(layer).id)
        if i == 0:
            raise EditError("there is no layer below")
        sotto = self.project.layers[i - 1]
        if sotto.locked:
            raise EditError(f"{sotto.name or sotto.id} is locked")
        sub = Project(width=self.project.width, height=self.project.height, background="transparent",
                      layers=[sotto, self.project.layers[i]], assets=self.project.assets)
        img = render.render(sub, self.base_dir, transparent=True)
        bbox = img.getbbox() or (0, 0, 1, 1)
        with self._edit():
            a = self._new_asset(img.crop(bbox), f"{sotto.name} (merged)", "merged")
            nuovo = Layer(id=sotto.id, type="image", name=sotto.name, x=float(bbox[0]), y=float(bbox[1]), asset=a.id)
            del self.project.layers[i]
            self.project.layers[i - 1] = nuovo
        return self.layer_view(nuovo)

    def flatten(self) -> dict:
        """Tutti i livelli visibili in uno (lo sfondo resta quello del documento)."""
        if not self.project.layers:
            raise EditError("nothing to flatten")
        img = render.render(self.project, self.base_dir, transparent=True)
        with self._edit():
            a = self._new_asset(img, "Flattened", "flat")
            self.project.layers = [Layer(id=new_id("L"), type="image", name="Flattened", asset=a.id)]
        return self.layer_view(self.project.layers[0])

    # ---- IA ----------------------------------------------------------------
    def _sorgente_layer(self, ly: Layer) -> Image.Image:
        """I pixel originali del contenuto del livello (asset ritagliato), senza trasformazioni."""
        if ly.type != "image":
            raise EditError(f"{ly.name or ly.id} is a {ly.type} layer: rasterize_layer it first")
        img = self._asset_img(ly.asset)
        if ly.crop:
            x, y, w, h = (int(round(v)) for v in ly.crop)
            img = img.crop((x, y, x + w, y + h))
        return img

    def remove_background(self, layer: str, keep: str = "subject", model: str = "isnet-general-use") -> dict:
        """Scontorno IA come maschera di livello (l'immagine resta intatta)."""
        ly = self._layer(layer)
        if keep not in ("subject", "background"):
            raise EditError("keep must be subject or background")
        src = self._asset_img(ly.asset) if ly.type == "image" else None
        if src is None:
            raise EditError(f"{ly.name or ly.id} is a {ly.type} layer: background removal works on images")
        try:
            m = ai.subject_mask(src, model)
        except ai.AIError as exc:
            raise EditError(str(exc)) from exc
        with self._edit():
            a = self._new_asset(m, f"{ly.name} subject mask", "mask")
            ly.mask = Mask(kind="image", asset=a.id, invert=(keep == "background"))
        return self.layer_view(ly)

    def select_subject_box(self, layer: str, model: str = "isnet-general-use") -> dict:
        """Riquadro del soggetto in pixel del documento (non modifica niente)."""
        ly = self._layer(layer, edit=False)
        src = self._sorgente_layer(ly)
        try:
            m = ai.subject_mask(src, model)
        except ai.AIError as exc:
            raise EditError(str(exc)) from exc
        bb = m.point(lambda v: 255 if v > 127 else 0).getbbox()
        if bb is None:
            return {"found": False}
        k = ly.scale
        return {"found": True, "box": [round(ly.x + bb[0] * k, 1), round(ly.y + bb[1] * k, 1),
                                       round((bb[2] - bb[0]) * k, 1), round((bb[3] - bb[1]) * k, 1)],
                "note": "box ignores rotation" if ly.rotation else ""}

    def _maschera_zona(self, box: list | None, ellipse: bool, mask_path: str | None,
                       feather: float) -> Image.Image:
        W, H = self.project.width, self.project.height
        if mask_path:
            m = render.load_image(mask_path)
            alpha = m.getchannel("A")
            m = alpha if alpha.getextrema()[0] < 255 else m.convert("L")
            m = m.resize((W, H), Image.Resampling.BILINEAR)
        else:
            if box is None:
                raise EditError("pass box=[x, y, w, h] (document pixels) or mask_path=")
            x, y, w, h = self._check_box(box)
            m = Image.new("L", (W, H), 0)
            (ImageDraw.Draw(m).ellipse if ellipse else ImageDraw.Draw(m).rectangle)([x, y, x + w, y + h], fill=255)
        if feather > 0:
            m = m.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(feather / 2))
        return m

    def inpaint(self, box: list | None = None, prompt: str | None = None, ellipse: bool = False,
                mask_path: str | None = None, feather: float = 6.0, backend: str = "auto",
                seed: int | None = None, name: str | None = None) -> dict:
        """Rimuove o rigenera una zona. Il risultato e' una toppa su un livello nuovo in cima.

        Senza prompt: rimozione classica (OpenCV o diffusione). Con prompt:
        riempimento generativo con ComfyUI. La toppa ha una maschera sfumata,
        quindi si sposta, si sfuma o si cancella come qualunque livello.
        """
        zona = self._maschera_zona(box, ellipse, mask_path, feather)
        bb = zona.getbbox()
        if bb is None:
            raise EditError("the area to fill is empty")
        W, H = self.project.width, self.project.height
        # contesto attorno alla zona: chi riempie deve vedere cosa c'e' intorno
        pad = int(max(bb[2] - bb[0], bb[3] - bb[1]) * 0.6) + 16
        cx0, cy0 = max(0, bb[0] - pad), max(0, bb[1] - pad)
        cx1, cy1 = min(W, bb[2] + pad), min(H, bb[3] + pad)
        comp = render.render(self.project, self.base_dir).convert("RGB").crop((cx0, cy0, cx1, cy1))
        m_crop = zona.crop((cx0, cy0, cx1, cy1))
        hard = m_crop.point(lambda v: 255 if v > 8 else 0)
        try:
            filled, motore = ai.fill(comp, hard, prompt, backend=backend, seed=seed)
        except ai.AIError as exc:
            raise EditError(str(exc)) from exc
        # la toppa si tiene stretta sulla zona (piu' la sfumatura): il contesto serviva solo a chi riempie
        mb = m_crop.getbbox() or (0, 0, *m_crop.size)
        patch = filled.convert("RGBA").crop(mb)
        with self._edit():
            a = self._new_asset(patch, "fill patch", "fill")
            ma = self._new_asset(m_crop.crop(mb), "fill patch mask", "fillmask")
            ly = Layer(id=new_id("L"), type="image", name=self._nome(name or ("Generative fill" if prompt else "Removed")),
                       asset=a.id, x=float(cx0 + mb[0]), y=float(cy0 + mb[1]),
                       mask=Mask(kind="image", asset=ma.id))
            self.project.layers.append(ly)
        return {**self.layer_view(ly), "backend": motore}

    def generate_image(self, prompt: str, width: int | None = None, height: int | None = None,
                       negative: str | None = None, seed: int | None = None, fit: str = "cover",
                       name: str | None = None) -> dict:
        """Genera un'immagine da testo (ComfyUI) e la mette su un livello nuovo."""
        if not prompt:
            raise EditError("prompt is empty")
        W, H = self.project.width, self.project.height
        k = 1024 / max(W, H)
        gw, gh = width or int(W * k), height or int(H * k)
        try:
            img = ai.generate(prompt, gw, gh, negative, seed)
        except (ai.AIError, RuntimeError) as exc:
            raise EditError(str(exc)) from exc
        with self._edit():
            a = self._new_asset(img, prompt[:40], "gen")
            out = self._add_image_inside(a, fit, name or "Generated")
        return out

    def _add_image_inside(self, a: Asset, fit: str, name: str) -> dict:
        W, H = self.project.width, self.project.height
        scale = max(W / a.width, H / a.height) if fit == "cover" else min(W / a.width, H / a.height)
        ly = Layer(id=new_id("L"), type="image", name=self._nome(name), asset=a.id, scale=scale,
                   x=(W - a.width * scale) / 2, y=(H - a.height * scale) / 2)
        self.project.layers.append(ly)
        return self.layer_view(ly)

    def upscale(self, layer: str, factor: float = 2.0, backend: str = "auto") -> dict:
        """Piu' pixel veri per un livello immagine; la dimensione sul documento non cambia."""
        ly = self._layer(layer)
        if not 1 < factor <= 8:
            raise EditError("factor must be in (1, 8]")
        src = self._sorgente_layer(ly)
        try:
            up, motore = ai.upscale(src, factor, backend)
        except (ai.AIError, RuntimeError) as exc:
            raise EditError(str(exc)) from exc
        k = up.width / src.width
        with self._edit():
            a = self._new_asset(up, f"{ly.name} x{factor:g}", "up")
            if ly.mask and ly.mask.kind == "image":
                # la maschera era allineata alla sorgente: si allinea alla nuova
                m = self._asset_img(ly.mask.asset)
                if ly.crop:
                    x, y, w, h = (int(round(v)) for v in ly.crop)
                    m = m.crop((x, y, x + w, y + h))
                ma = self._new_asset(m.resize(up.size, Image.Resampling.BILINEAR), f"{ly.name} mask", "mask")
                ly.mask.asset = ma.id
            ly.asset, ly.crop = a.id, None
            ly.scale = ly.scale / k
        return {**self.layer_view(ly), "backend": motore, "pixels": [up.width, up.height]}

    def auto_enhance(self, layer: str | None = None, strength: float = 1.0) -> dict:
        """Misura l'immagine e aggiunge le correzioni come filtri normali (o regolazione in cima)."""
        if not 0 < strength <= 2:
            raise EditError("strength must be in (0, 2]")
        s = min(1.0, 1200 / max(self.project.width, 1))
        if layer is None:
            arr = render.composite(self.project, self.base_dir, scale=s)
        else:
            ly = self._layer(layer)
            arr = fx.to_np(render.layer_image(self.project, ly.id, self.base_dir, scale=s))
        chain = analyze.suggest_enhance(arr, strength)
        if not chain:
            return {"changed": False, "note": "the image is already balanced"}
        if layer is None:
            out = self.add_adjustment(effects=chain, name="Auto enhance")
        else:
            ly = self._layer(layer)
            with self._edit():
                for e in chain:
                    ly.effects.append(Effect(type=e["type"], params=fx.validate(e["type"], e["params"])))
            out = self.layer_view(ly)
        return {**out, "applied": chain}

    def match_color(self, layer: str, reference: str, amount: float = 1.0) -> dict:
        """Porta colori e tono di ``layer`` verso quelli di ``reference`` (trasferimento statistico)."""
        ly = self._layer(layer)
        ref = self._layer(reference, edit=False)
        s = min(1.0, 800 / max(self.project.width, 1))
        a_src = fx.to_np(render.layer_image(self.project, ly.id, self.base_dir, scale=s))
        a_ref = fx.to_np(render.layer_image(self.project, ref.id, self.base_dir, scale=s))
        sm, ss = analyze.channel_stats(a_src)
        dm, ds = analyze.channel_stats(a_ref)
        params = fx.validate("color_match", {"src_mean": sm, "src_std": ss, "dst_mean": dm,
                                             "dst_std": ds, "amount": amount})
        with self._edit():
            ly.effects = [e for e in ly.effects if e.type != "color_match"]
            ly.effects.append(Effect(type="color_match", params=params))
        return self.layer_view(ly)

    # ---- esportazione --------------------------------------------------------
    def export_image(self, path: str, quality: int = 92, scale: float = 1.0, transparent: bool = False,
                     box: list | None = None) -> dict:
        if not 0 < scale <= 4:
            raise EditError("scale must be in (0, 4]")
        img = render.render(self.project, self.base_dir, scale=scale, transparent=transparent)
        if box is not None:
            x0, y0, x1, y1 = self._clip_box(self._check_box(box))
            img = img.crop((int(x0 * scale), int(y0 * scale), int(x1 * scale), int(y1 * scale)))
        try:
            out = render.save_image(img, path, quality=quality,
                                    background=self.project.background if not transparent else "transparent",
                                    dpi=self.project.dpi)
        except ValueError as exc:
            raise EditError(str(exc)) from exc
        return {"path": str(out), "width": img.width, "height": img.height, "bytes": out.stat().st_size}


def asarray(img: Image.Image) -> np.ndarray:
    return fx.to_np(img)
