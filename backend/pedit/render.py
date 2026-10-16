"""Documento -> pixel.

Il render e' una funzione pura del documento: niente stato, niente casualita'
(il rumore ha il suo seme). L'anteprima nella UI, l'immagine che guarda
l'agente e il file esportato escono da qui, quindi quello che si vede e' quello
che si esporta.

Ogni livello si disegna su una tela trasparente grande quanto il documento,
poi: filtri -> maschera -> stili -> opacita' -> fusione con cio' che sta sotto.
Un livello di regolazione invece prende il composito sotto di lui, ci applica
i suoi filtri e lo rimescola attraverso la propria maschera.

``scale`` serve alle anteprime: si renderizza tutto piu' piccolo, e le
lunghezze in pixel (sfocature, ombre, contorni, testo) scalano con lui.
"""

from __future__ import annotations

import copy
import math
import os
import sys
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from . import effects as fx
from .model import IMAGE_EXTS, Layer, Project

# --------------------------------------------------------------------------
# asset su disco (con cache)
# --------------------------------------------------------------------------

_CACHE: "OrderedDict[tuple, Image.Image]" = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 24


def resolve(path: str, base_dir: str | None) -> Path:
    p = Path(path)
    if not p.is_absolute() and base_dir:
        p = Path(base_dir) / p
    return p


def load_image(path: str | Path) -> Image.Image:
    """Immagine RGBA, con la rotazione EXIF applicata. Tenuta in cache per mtime."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError as exc:
        raise FileNotFoundError(f"image not found: {p}") from exc
    key = (str(p), st.st_mtime_ns, st.st_size)
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    with Image.open(p) as im:
        im = ImageOps.exif_transpose(im)
        img = im.convert("RGBA")
    with _CACHE_LOCK:
        _CACHE[key] = img
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return img


# --------------------------------------------------------------------------
# font
# --------------------------------------------------------------------------

_FAMIGLIE = {
    "sans": ["arial", "segoeui", "helvetica", "DejaVuSans", "LiberationSans-Regular", "Roboto-Regular"],
    "serif": ["times", "georgia", "DejaVuSerif", "LiberationSerif-Regular"],
    "mono": ["consola", "cour", "DejaVuSansMono", "LiberationMono-Regular"],
    "display": ["impact", "Anton-Regular", "BebasNeue-Regular", "arialbd", "DejaVuSans-Bold"],
    "handwriting": ["comic", "segoepr", "DejaVuSans"],
}
_VARIANTI = {
    # (bold, italic) -> suffissi tipici dei file di Windows e Linux
    (True, False): ["bd", "b", "-Bold", "Bold"],
    (False, True): ["i", "-Italic", "-Oblique", "Italic"],
    (True, True): ["bi", "z", "-BoldItalic", "-BoldOblique", "BoldItalic"],
}


def _cartelle_font() -> list[Path]:
    out = []
    if sys.platform == "win32":
        out.append(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            out.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    elif sys.platform == "darwin":
        out += [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library/Fonts"]
    else:
        out += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts",
                Path.home() / ".local/share/fonts"]
    out.append(Path(__file__).resolve().parent / "fonts")
    return [p for p in out if p.is_dir()]


_INDICE: dict[str, Path] | None = None


def _indice_font() -> dict[str, Path]:
    global _INDICE
    if _INDICE is None:
        idx: dict[str, Path] = {}
        for d in _cartelle_font():
            for f in d.rglob("*"):
                if f.suffix.lower() in (".ttf", ".otf", ".ttc"):
                    idx.setdefault(f.stem.lower(), f)
        _INDICE = idx
    return _INDICE


def font_path(font: str, bold: bool = False, italic: bool = False) -> Path | None:
    """Famiglia ('sans', 'serif'...), nome di file ('arial') o percorso."""
    if font and Path(font).suffix.lower() in (".ttf", ".otf", ".ttc") and Path(font).is_file():
        return Path(font)
    idx = _indice_font()
    nomi = _FAMIGLIE.get(font, [font] + _FAMIGLIE["sans"])
    for nome in nomi:
        base = nome.lower()
        if bold or italic:
            for suf in _VARIANTI[(bold, italic)]:
                if (base + suf.lower()) in idx:
                    return idx[base + suf.lower()]
        if base in idx:
            return idx[base]
    return None


def list_fonts() -> list[str]:
    return sorted({*(_FAMIGLIE), *(k for k in _indice_font())})


def get_font(font: str, size: float, bold: bool = False, italic: bool = False) -> ImageFont.FreeTypeFont:
    size = max(1, int(round(size)))
    p = font_path(font, bold, italic)
    if p is not None:
        try:
            return ImageFont.truetype(str(p), size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def _a_capo(testo: str, font, max_w: float, draw: ImageDraw.ImageDraw) -> str:
    if max_w <= 0:
        return testo
    righe = []
    for par in testo.split("\n"):
        cur = ""
        for parola in par.split(" "):
            prova = (cur + " " + parola).strip()
            if cur and draw.textlength(prova, font=font) > max_w:
                righe.append(cur)
                cur = parola
            else:
                cur = prova
        righe.append(cur)
    return "\n".join(righe)


# --------------------------------------------------------------------------
# contenuto dei livelli, gia' alla scala giusta
# --------------------------------------------------------------------------


def _text_image(layer: Layer, k: float) -> tuple[Image.Image, tuple[float, float]]:
    """Il testo si disegna direttamente alla dimensione finale: niente sgranature."""
    st = layer.style
    font = get_font(st.font, st.size * k, st.bold, st.italic)
    stroke = int(round(st.stroke_width * k))
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    testo = _a_capo(layer.text or "", font, st.max_width * k, tmp)
    spacing = max(0, int(round((st.line_spacing - 1.0) * st.size * k)))
    box = tmp.multiline_textbbox((0, 0), testo, font=font, spacing=spacing, align=st.align,
                                 stroke_width=stroke)
    w, h = max(1, box[2] - box[0]), max(1, box[3] - box[1])
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.multiline_text((-box[0], -box[1]), testo, font=font, fill=fx.parse_color(st.color),
                     spacing=spacing, align=st.align, stroke_width=stroke,
                     stroke_fill=fx.parse_color(st.stroke_color))
    return img, (w / k, h / k)


def text_size(layer: Layer) -> tuple[float, float]:
    """Ingombro del testo in pixel del documento (alla scala del livello)."""
    _, (w, h) = _text_image(layer, 1.0)
    return w, h


def _shape_image(layer: Layer, k: float) -> tuple[Image.Image, tuple[float, float]]:
    sh = layer.shape
    sw = sh.stroke_width * k if sh.stroke else 0.0
    pad = int(math.ceil(sw / 2)) + 1
    w, h = max(1, int(round(sh.w * k))), max(1, int(round(sh.h * k)))
    # 4x e poi giu': bordi lisci senza dipendere dall'antialias di ImageDraw
    ss = 4
    img = Image.new("RGBA", ((w + 2 * pad) * ss, (h + 2 * pad) * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fill = fx.parse_color(sh.fill) if sh.fill else None
    outline = fx.parse_color(sh.stroke) if sh.stroke and sw > 0 else None
    width = int(round(sw * ss)) if outline else 0
    box = [pad * ss, pad * ss, (pad + w) * ss - 1, (pad + h) * ss - 1]
    if sh.kind == "rect":
        r = sh.radius * k * ss
        if r > 0:
            d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)
        else:
            d.rectangle(box, fill=fill, outline=outline, width=width)
    elif sh.kind == "ellipse":
        d.ellipse(box, fill=fill, outline=outline, width=width)
    elif sh.kind in ("line", "polygon"):
        pts = sh.points or [[0, 0], [sh.w, sh.h]]
        xy = [((pad + px * k) * ss, (pad + py * k) * ss) for px, py in pts]
        if sh.kind == "line":
            col = outline or fill or (255, 255, 255, 255)
            d.line(xy, fill=col, width=max(1, int(round(max(sw, 2 * k) * ss))), joint="curve")
        else:
            d.polygon(xy, fill=fill, outline=outline, width=width)
    img = img.resize((w + 2 * pad, h + 2 * pad), Image.Resampling.LANCZOS)
    # il padding (per il contorno) e' simmetrico: centrando l'immagine sul centro
    # della forma, la forma cade esattamente in x, y
    return img, (sh.w, sh.h)


def _image_content(layer: Layer, project: Project, base_dir: str | None,
                   k: float) -> tuple[Image.Image, tuple[float, float]]:
    a = project.asset(layer.asset)
    img = load_image(resolve(a.path, base_dir))
    if layer.crop:
        x, y, w, h = (int(round(v)) for v in layer.crop)
        img = img.crop((x, y, x + w, y + h))
    w0, h0 = img.size
    tw, th = max(1, int(round(w0 * k))), max(1, int(round(h0 * k)))
    if (tw, th) != (w0, h0):
        img = img.resize((tw, th), Image.Resampling.LANCZOS if k < 1 else Image.Resampling.BICUBIC)
    return img, (float(w0), float(h0))


def content_size(layer: Layer, project: Project, base_dir: str | None = None) -> tuple[float, float]:
    """Dimensione del contenuto in pixel del documento, gia' moltiplicata per ``scale``."""
    if layer.type == "image":
        a = project.asset(layer.asset)
        if layer.crop:
            w, h = layer.crop[2], layer.crop[3]
        else:
            w, h = a.width, a.height
    elif layer.type == "text":
        w, h = text_size(layer)
    elif layer.type == "shape":
        w, h = layer.shape.w, layer.shape.h
    else:
        return float(project.width), float(project.height)
    return w * layer.scale, h * layer.scale


def _fill_image(layer: Layer, W: int, H: int) -> Image.Image:
    f = layer.fill
    c1 = np.array(fx.parse_color(f.color), np.float32) / 255
    if f.kind == "solid":
        arr = np.broadcast_to(c1, (H, W, 4)).copy()
        return fx.to_pil(arr)
    c2 = np.array(fx.parse_color(f.color2), np.float32) / 255
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    if f.kind == "linear":
        ang = math.radians(f.angle)
        dx, dy = math.sin(ang), -math.cos(ang)    # 90 = da sinistra a destra, 180 = dall'alto in basso
        proj = (xx - W / 2) * dx + (yy - H / 2) * dy
        half = abs(W / 2 * dx) + abs(H / 2 * dy)
        t = np.clip(proj / max(2 * half, 1e-6) + 0.5, 0, 1)
    else:
        cx, cy = f.cx * W, f.cy * H
        r = math.hypot(max(cx, W - cx), max(cy, H - cy))
        t = np.clip(np.hypot(xx - cx, yy - cy) / max(r, 1e-6), 0, 1)
    arr = c1 * (1 - t[..., None]) + c2 * t[..., None]
    return fx.to_pil(arr.astype(np.float32))


def _stroke_alpha(st, W: int, H: int, s: float) -> np.ndarray:
    """Copertura di una pennellata (0..1), durezza e opacita' comprese."""
    m = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(m)
    size = max(1.0, st.size * s)
    pts = [(px * s, py * s) for px, py in st.points]
    if len(pts) == 1:
        x, y = pts[0]
        d.ellipse([x - size / 2, y - size / 2, x + size / 2, y + size / 2], fill=255)
    else:
        d.line(pts, fill=255, width=max(1, int(round(size))), joint="curve")
        for x, y in (pts[0], pts[-1]):
            d.ellipse([x - size / 2, y - size / 2, x + size / 2, y + size / 2], fill=255)
    soft = (1 - st.hardness) * size / 2
    if soft > 0.3:
        m = m.filter(ImageFilter.GaussianBlur(soft))
    return np.asarray(m, np.float32) / 255 * st.opacity


def _sposta(arr: np.ndarray, dx: int, dy: int) -> np.ndarray:
    """``out[y, x] = arr[y + dy, x + dx]``; fuori dalla tela e' trasparente."""
    H, W = arr.shape[:2]
    out = np.zeros_like(arr)
    xs0, xs1 = max(0, dx), min(W, W + dx)
    ys0, ys1 = max(0, dy), min(H, H + dy)
    if xs1 <= xs0 or ys1 <= ys0:
        return out
    out[ys0 - dy:ys1 - dy, xs0 - dx:xs1 - dx] = arr[ys0:ys1, xs0:xs1]
    return out


def _sfoca(arr: np.ndarray, r: float) -> np.ndarray:
    if r <= 0.3:
        return arr
    # la bassa frequenza perdona gli 8 bit: basta Pillow
    im = Image.fromarray((np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8), "RGB")
    return np.asarray(im.filter(ImageFilter.GaussianBlur(r)), np.float32) / 255


def _clone_top(st, sotto: np.ndarray, a: np.ndarray, s: float) -> np.ndarray:
    """I pixel del timbro: ``sotto`` spostato di ``clone``; con heal, rifusi sulla luce d'arrivo."""
    dx, dy = (int(round(v * s)) for v in st.clone)
    src = _sposta(sotto, dx, dy)
    if st.heal:
        # bassa frequenza dalla zona di arrivo, dettaglio dalla sorgente
        r = max(1.0, st.size * s / 3)
        rgb = np.clip(src[..., :3] + _sfoca(sotto[..., :3], r) - _sfoca(src[..., :3], r), 0, 1)
        src = np.concatenate([rgb, src[..., 3:4]], axis=-1)
    top = src.copy()
    top[..., 3] = a * src[..., 3]
    return top


def _paint_image(layer: Layer, W: int, H: int, s: float, sotto: np.ndarray | None = None) -> Image.Image:
    """Pennellate in ordine; ``erase`` toglie alpha a cio' che e' gia' dipinto.

    Una pennellata ``clone`` copia da ``sotto`` (il composito sotto il livello);
    senza ``sotto`` non disegna niente.
    """
    canvas = np.zeros((H, W, 4), np.float32)
    for st in layer.strokes:
        if not st.points:
            continue
        a = _stroke_alpha(st, W, H, s)
        if st.erase:
            canvas[..., 3] *= 1 - a
            continue
        if st.clone:
            if sotto is None:
                continue
            top = _clone_top(st, sotto, a, s)
        else:
            col = np.array(fx.parse_color(st.color), np.float32) / 255
            top = np.empty_like(canvas)
            top[..., :3] = col[:3]
            top[..., 3] = a * col[3]
        canvas = fx._over(top, canvas)
    return fx.to_pil(canvas)


def has_clone(layer: Layer) -> bool:
    return layer.type == "paint" and any(st.clone for st in layer.strokes)


# --------------------------------------------------------------------------
# posizionamento e maschere
# --------------------------------------------------------------------------


def _trasforma(img: Image.Image, layer: Layer, native: tuple[float, float], s: float,
               W: int, H: int, mode: str = "RGBA") -> Image.Image:
    """Ribalta, ruota e posa ``img`` (gia' scalato) su una tela grande quanto il documento."""
    if layer.flip_h:
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if layer.flip_v:
        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    if layer.rotation % 360:
        # positivo = orario, come in Photoshop; PIL ruota in senso antiorario
        img = img.rotate(-layer.rotation, resample=Image.Resampling.BICUBIC, expand=True)
    cx = (layer.x + native[0] * layer.scale / 2) * s
    cy = (layer.y + native[1] * layer.scale / 2) * s
    canvas = Image.new(mode, (W, H), 0)
    canvas.paste(img, (int(round(cx - img.width / 2)), int(round(cy - img.height / 2))))
    return canvas


def _mask_array(layer: Layer, project: Project, base_dir: str | None, s: float, W: int, H: int,
                native: tuple[float, float] | None, content_px: tuple[int, int] | None) -> np.ndarray | None:
    m = layer.mask
    if m is None:
        return None
    if m.kind in ("rect", "ellipse"):
        im = Image.new("L", (W, H), 0)
        x, y, w, h = (v * s for v in (m.box or [0, 0, project.width, project.height]))
        d = ImageDraw.Draw(im)
        (d.rectangle if m.kind == "rect" else d.ellipse)([x, y, x + w, y + h], fill=255)
    elif m.kind == "paint":
        im = Image.new("L", (W, H), int(round(255 * min(1.0, max(0.0, m.base)))))
    elif m.kind == "image":
        a = project.asset(m.asset)
        mim = load_image(resolve(a.path, base_dir))
        # una maschera RGBA usa il suo alpha se ce l'ha, altrimenti la luminanza
        alpha = mim.getchannel("A")
        mim = alpha if alpha.getextrema()[0] < 255 else mim.convert("L")
        if layer.type == "image" and layer.crop:
            cx, cy, cw, ch = (int(round(v)) for v in layer.crop)
            src_a = project.asset(layer.asset)
            if mim.size != (src_a.width, src_a.height):
                mim = mim.resize((src_a.width, src_a.height), Image.Resampling.BILINEAR)
            mim = mim.crop((cx, cy, cx + cw, cy + ch))
        if native is not None and content_px is not None and layer.type in ("image", "text", "shape"):
            mim = mim.resize(content_px, Image.Resampling.BILINEAR)
            im = _trasforma(mim, layer, native, s, W, H, mode="L")
        else:
            im = mim.resize((W, H), Image.Resampling.BILINEAR)
    else:
        return None
    if m.strokes:
        arr = np.asarray(im, np.float32) / 255
        for st in m.strokes:
            if not st.points:
                continue
            a = _stroke_alpha(st, W, H, s)
            r, g, b, _ = fx.parse_color(st.color)
            # erase su una maschera = torna visibile, come il bianco
            v = 1.0 if st.erase else (r + g + b) / 765
            arr = arr * (1 - a) + v * a
        im = Image.fromarray((np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8), "L")
    if m.feather > 0:
        im = im.filter(ImageFilter.GaussianBlur(m.feather * s))
    arr = np.asarray(im, np.float32) / 255
    if m.invert:
        arr = 1 - arr
    return arr * m.density


# --------------------------------------------------------------------------
# fusioni (W3C compositing, alpha non premoltiplicato)
# --------------------------------------------------------------------------


def _lum(c: np.ndarray) -> np.ndarray:
    return 0.3 * c[..., 0:1] + 0.59 * c[..., 1:2] + 0.11 * c[..., 2:3]


def _clip_color(c: np.ndarray) -> np.ndarray:
    lu = _lum(c)
    n = c.min(axis=-1, keepdims=True)
    x = c.max(axis=-1, keepdims=True)
    c = np.where(n < 0, lu + (c - lu) * lu / np.maximum(lu - n, 1e-6), c)
    c = np.where(x > 1, lu + (c - lu) * (1 - lu) / np.maximum(x - lu, 1e-6), c)
    return c


def _set_lum(c: np.ndarray, lu: np.ndarray) -> np.ndarray:
    return _clip_color(c + (lu - _lum(c)))


def _sat(c: np.ndarray) -> np.ndarray:
    return c.max(axis=-1, keepdims=True) - c.min(axis=-1, keepdims=True)


def _set_sat(c: np.ndarray, s: np.ndarray) -> np.ndarray:
    """W3C SetSat: il massimo va a ``s``, il minimo a 0, il medio in proporzione."""
    n = c.min(axis=-1, keepdims=True)
    span = c.max(axis=-1, keepdims=True) - n
    return np.where(span > 1e-6, (c - n) * s / np.maximum(span, 1e-6), 0.0)


def _blend_fn(mode: str, cb: np.ndarray, cs: np.ndarray) -> np.ndarray:
    if mode in ("normal", "dissolve"):
        return cs
    if mode == "linear_burn":
        return np.maximum(0.0, cb + cs - 1)
    if mode == "divide":
        return np.where(cs <= 0, np.where(cb > 0, 1.0, 0.0), np.minimum(1.0, cb / np.maximum(cs, 1e-6)))
    if mode == "vivid_light":
        return np.where(cs <= 0.5, _blend_fn("color_burn", cb, 2 * cs), _blend_fn("color_dodge", cb, 2 * cs - 1))
    if mode == "linear_light":
        return np.clip(cb + 2 * cs - 1, 0, 1)
    if mode == "pin_light":
        return np.where(cs <= 0.5, np.minimum(cb, 2 * cs), np.maximum(cb, 2 * cs - 1))
    if mode == "hard_mix":
        return np.where(cb + cs >= 1, 1.0, 0.0)
    if mode in ("darker_color", "lighter_color"):
        prendi = _lum(cs) < _lum(cb) if mode == "darker_color" else _lum(cs) > _lum(cb)
        return np.where(prendi, cs, cb)
    if mode == "hue":
        return _set_lum(_set_sat(cs, _sat(cb)), _lum(cb))
    if mode == "saturation":
        return _set_lum(_set_sat(cb, _sat(cs)), _lum(cb))
    if mode == "color":
        return _set_lum(cs, _lum(cb))
    if mode == "luminosity":
        return _set_lum(cb, _lum(cs))
    if mode == "multiply":
        return cb * cs
    if mode == "screen":
        return cb + cs - cb * cs
    if mode == "overlay":
        return _blend_fn("hard_light", cs, cb)
    if mode == "hard_light":
        return np.where(cs <= 0.5, cb * 2 * cs, _blend_fn("screen", cb, 2 * cs - 1))
    if mode == "soft_light":
        d = np.where(cb <= 0.25, ((16 * cb - 12) * cb + 4) * cb, np.sqrt(np.clip(cb, 0, 1)))
        return np.where(cs <= 0.5, cb - (1 - 2 * cs) * cb * (1 - cb), cb + (2 * cs - 1) * (d - cb))
    if mode == "darken":
        return np.minimum(cb, cs)
    if mode == "lighten":
        return np.maximum(cb, cs)
    if mode == "difference":
        return np.abs(cb - cs)
    if mode == "exclusion":
        return cb + cs - 2 * cb * cs
    if mode == "color_dodge":
        return np.where(cs >= 1, 1.0, np.minimum(1.0, cb / np.maximum(1 - cs, 1e-6)))
    if mode == "color_burn":
        return np.where(cs <= 0, 0.0, 1 - np.minimum(1.0, (1 - cb) / np.maximum(cs, 1e-6)))
    if mode == "add":
        return np.minimum(1.0, cb + cs)
    if mode == "subtract":
        return np.maximum(0.0, cb - cs)
    raise ValueError(f"unknown blend mode {mode!r}")


def _dissolvi(alpha: np.ndarray) -> np.ndarray:
    """Dissolve: ogni pixel e' tutto o niente, con probabilita' = alpha. Seme fisso: render puro."""
    soglia = np.random.default_rng(1234).random(alpha.shape[:2], dtype=np.float32)[..., None]
    return (soglia < alpha).astype(np.float32)


def blend(bottom: np.ndarray, top: np.ndarray, mode: str = "normal", opacity: float = 1.0) -> np.ndarray:
    ab = bottom[..., 3:4]
    as_ = top[..., 3:4] * opacity
    if mode == "dissolve":
        as_ = _dissolvi(as_)
    cb, cs = bottom[..., :3], np.clip(top[..., :3], 0, 1)
    mixed = (1 - ab) * cs + ab * np.clip(_blend_fn(mode, cb, cs), 0, 1)
    ao = as_ + ab * (1 - as_)
    co = as_ * mixed + ab * (1 - as_) * cb
    rgb = np.where(ao > 1e-6, co / np.maximum(ao, 1e-6), 0)
    return np.concatenate([rgb, ao], axis=-1).astype(np.float32)


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------


def _applica_effetti(arr: np.ndarray, layer: Layer, kind: str, s: float) -> np.ndarray:
    for e in layer.effects:
        if not e.enabled:
            continue
        sp = fx.spec(e.type)
        if sp["kind"] != kind:
            continue
        arr = fx.apply(arr, e.type, e.params, scale=s)
    return arr


def layer_canvas(layer: Layer, project: Project, base_dir: str | None, s: float,
                 W: int, H: int, sotto: np.ndarray | None = None) -> np.ndarray:
    """Il livello da solo, posato su una tela trasparente del documento, maschera e stili inclusi.

    ``sotto`` e' il composito sotto il livello: serve al timbro clone.
    """
    native = None
    px = None
    if layer.type == "image":
        img, native = _image_content(layer, project, base_dir, layer.scale * s)
        px = img.size
        canvas = _trasforma(img, layer, native, s, W, H)
    elif layer.type == "text":
        img, native = _text_image(layer, layer.scale * s)
        px = img.size
        canvas = _trasforma(img, layer, native, s, W, H)
    elif layer.type == "shape":
        img, native = _shape_image(layer, layer.scale * s)
        px = img.size
        canvas = _trasforma(img, layer, native, s, W, H)
    elif layer.type == "fill":
        canvas = _fill_image(layer, W, H)
    elif layer.type == "paint":
        canvas = _paint_image(layer, W, H, s, sotto)
    elif layer.type == "group":
        # gruppo isolato: i figli su trasparente, poi il gruppo come un livello solo
        canvas = fx.to_pil(_componi(layer.children, project, base_dir, s, np.zeros((H, W, 4), np.float32)))
    else:
        raise ValueError(f"cannot draw a {layer.type} layer on its own")
    arr = fx.to_np(canvas)
    arr = _applica_effetti(arr, layer, "filter", s)
    m = _mask_array(layer, project, base_dir, s, W, H, native, px)
    if m is not None:
        arr[..., 3] *= m
    return _applica_effetti(arr, layer, "style", s)


def _sfondo(project: Project, W: int, H: int, transparent: bool) -> np.ndarray:
    if transparent:
        return np.zeros((H, W, 4), np.float32)
    c = np.array(fx.parse_color(project.background), np.float32) / 255
    return np.broadcast_to(c, (H, W, 4)).copy()


def _regola(out: np.ndarray, layer: Layer, project: Project, base_dir: str | None, s: float,
            W: int, H: int) -> np.ndarray:
    """Un livello di regolazione su ``out``: filtri, poi rimescolato con maschera e opacita'."""
    adj = out.copy()
    for e in layer.effects:
        if e.enabled:
            adj = fx.apply(adj, e.type, e.params, scale=s)
    m = _mask_array(layer, project, base_dir, s, W, H, None, None)
    k = layer.opacity * (m[..., None] if m is not None else 1.0)
    adj[..., 3] = out[..., 3]
    if layer.blend == "normal":
        return out * (1 - k) + adj * k
    top = adj.copy()
    top[..., 3] = (k if np.ndim(k) == 0 else k[..., 0]) * out[..., 3]
    return blend(out, top, layer.blend, 1.0)


def _passante(out: np.ndarray, layer: Layer, project: Project, base_dir: str | None, s: float,
              W: int, H: int) -> np.ndarray:
    """Gruppo pass-through: i figli lavorano su ``out``; maschera e opacita' dosano il risultato."""
    dopo = _componi(layer.children, project, base_dir, s, out)
    m = _mask_array(layer, project, base_dir, s, W, H, None, None)
    k = layer.opacity * (m[..., None] if m is not None else 1.0)
    if np.ndim(k) == 0 and k >= 1:
        return dopo
    return out * (1 - k) + dopo * k


def _componi(layers: list[Layer], project: Project, base_dir: str | None, s: float,
             out: np.ndarray) -> np.ndarray:
    """Compone una lista di livelli (dal basso) sopra ``out``.

    Una maschera di ritaglio e' una serie: la base e i livelli ``clip`` subito
    sopra si compongono fra loro dentro l'alpha della base, e la serie si fonde
    poi con il modo e l'opacita' della base, come in Photoshop.
    """
    H, W = out.shape[:2]
    i = 0
    while i < len(layers):
        base = layers[i]
        j = i + 1
        while j < len(layers) and layers[j].clip:
            j += 1
        agganciati = [x for x in layers[i + 1:j] if x.visible]
        if not base.visible:
            # una base nascosta nasconde tutta la sua serie
            i = j
            continue
        if base.type == "adjustment":
            # a una regolazione non si aggancia niente: i clip sopra contano come livelli normali
            out = _regola(out, base, project, base_dir, s, W, H)
            if agganciati:
                out = _componi([_sciolto(x) for x in agganciati], project, base_dir, s, out)
            i = j
            continue
        if base.type == "group" and base.blend == "pass_through" and not agganciati:
            out = _passante(out, base, project, base_dir, s, W, H)
            i = j
            continue
        top = layer_canvas(base, project, base_dir, s, W, H, sotto=out if has_clone(base) else None)
        if agganciati:
            alpha = top[..., 3:4].copy()
            serie = top.copy()
            serie[..., 3] = 1.0
            for x in agganciati:
                if x.type == "adjustment":
                    serie = _regola(serie, x, project, base_dir, s, W, H)
                    continue
                c = layer_canvas(x, project, base_dir, s, W, H, sotto=serie if has_clone(x) else None)
                serie = blend(serie, c, "normal" if x.blend == "pass_through" else x.blend, x.opacity)
            serie[..., 3:4] = alpha
            top = serie
        out = blend(out, top, "normal" if base.blend == "pass_through" else base.blend, base.opacity)
        i = j
    return out


def _sciolto(layer: Layer) -> Layer:
    x = copy.copy(layer)
    x.clip = False
    return x


def composite(project: Project, base_dir: str | None = None, scale: float = 1.0,
              upto: int | None = None, transparent: bool = False, skip: set[str] | None = None) -> np.ndarray:
    """Composito dei livelli in cima ``[0, upto)`` come array float. ``skip``: id da saltare."""
    W = max(1, int(round(project.width * scale)))
    H = max(1, int(round(project.height * scale)))
    out = _sfondo(project, W, H, transparent)
    layers = project.layers if upto is None else project.layers[:upto]
    if skip:
        layers = [ly for ly in layers if ly.id not in skip]
    return _componi(layers, project, base_dir, scale, out)


def composite_below(project: Project, layer_id: str, base_dir: str | None = None, scale: float = 1.0,
                    transparent: bool = False) -> np.ndarray:
    """Cio' che vede un livello sotto di se', anche dentro un gruppo.

    Dentro un gruppo isolato si parte da trasparente; in uno pass-through da
    quello che c'e' sotto il gruppo.
    """
    W = max(1, int(round(project.width * scale)))
    H = max(1, int(round(project.height * scale)))
    out = _sfondo(project, W, H, transparent)
    catena = []
    cur: Layer | None = project.layer(layer_id)
    while cur is not None:
        catena.append(cur)
        cur = project.parent(cur.id)
    lista = project.layers
    for nodo in reversed(catena):
        i = next(k for k, ly in enumerate(lista) if ly is nodo)
        out = _componi(lista[:i], project, base_dir, scale, out)
        if nodo is not catena[0]:
            if nodo.blend != "pass_through":
                out = np.zeros_like(out)
            lista = nodo.children
    return out


def render(project: Project, base_dir: str | None = None, scale: float = 1.0,
           transparent: bool = False, upto: int | None = None) -> Image.Image:
    return fx.to_pil(composite(project, base_dir, scale, upto=upto, transparent=transparent))


def render_width(project: Project, width: int, base_dir: str | None = None, **kw) -> Image.Image:
    """Anteprima larga ``width`` pixel (mai piu' grande dell'originale)."""
    s = min(1.0, width / max(project.width, 1))
    return render(project, base_dir, scale=s, **kw)


def layer_image(project: Project, layer_id: str, base_dir: str | None = None,
                scale: float = 1.0) -> Image.Image:
    """Un livello da solo su trasparente (regolazioni: l'effetto su cio' che sta sotto)."""
    layer = project.layer(layer_id)
    W = max(1, int(round(project.width * scale)))
    H = max(1, int(round(project.height * scale)))
    if layer.type == "adjustment":
        sotto = composite_below(project, layer.id, base_dir, scale)
        return fx.to_pil(_regola(sotto, layer, project, base_dir, scale, W, H))
    sotto = composite_below(project, layer.id, base_dir, scale) if has_clone(layer) else None
    return fx.to_pil(layer_canvas(layer, project, base_dir, scale, W, H, sotto=sotto))


# --------------------------------------------------------------------------
# esportazione
# --------------------------------------------------------------------------

FORMATI = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP", ".tif": "TIFF",
           ".tiff": "TIFF", ".bmp": "BMP", ".gif": "GIF"}
if ".avif" in IMAGE_EXTS:
    FORMATI[".avif"] = "AVIF"


def save_image(img: Image.Image, path: str | Path, quality: int = 92, background: str = "#ffffff",
               dpi: int | None = None) -> Path:
    p = Path(path)
    fmt = FORMATI.get(p.suffix.lower())
    if fmt is None:
        raise ValueError(f"unsupported format {p.suffix!r}; use {', '.join(FORMATI)}")
    p.parent.mkdir(parents=True, exist_ok=True)
    out = img
    if fmt in ("JPEG", "BMP"):
        # niente alpha: si appoggia su uno sfondo pieno
        bg = Image.new("RGBA", img.size, fx.parse_color(background) if background != "transparent"
                       else (255, 255, 255, 255))
        out = Image.alpha_composite(bg, img.convert("RGBA")).convert("RGB")
    kw: dict = {}
    if fmt in ("JPEG", "WEBP", "AVIF"):
        kw["quality"] = int(quality)
    if fmt == "JPEG":
        kw["optimize"] = True
    if dpi and fmt in ("JPEG", "PNG", "TIFF"):
        kw["dpi"] = (dpi, dpi)
    tmp = p.with_name(f"~{p.stem}.tmp{p.suffix}")
    out.save(tmp, fmt, **kw)
    os.replace(tmp, p)
    return p
