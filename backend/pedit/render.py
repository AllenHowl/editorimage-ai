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

import math
import os
import sys
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from . import effects as fx
from .model import Layer, Project

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


def _paint_image(layer: Layer, W: int, H: int, s: float) -> Image.Image:
    """Pennellate in ordine; ``erase`` toglie alpha a cio' che e' gia' dipinto."""
    canvas = np.zeros((H, W, 4), np.float32)
    for st in layer.strokes:
        if not st.points:
            continue
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
        a = np.asarray(m, np.float32) / 255 * st.opacity
        if st.erase:
            canvas[..., 3] *= 1 - a
            continue
        col = np.array(fx.parse_color(st.color), np.float32) / 255
        a = a * col[3]
        top = np.empty_like(canvas)
        top[..., :3] = col[:3]
        top[..., 3] = a
        canvas = fx._over(top, canvas)
    return fx.to_pil(canvas)


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
    if m.feather > 0:
        im = im.filter(ImageFilter.GaussianBlur(m.feather * s))
    arr = np.asarray(im, np.float32) / 255
    if m.invert:
        arr = 1 - arr
    return arr * m.density


# --------------------------------------------------------------------------
# fusioni (W3C compositing, alpha non premoltiplicato)
# --------------------------------------------------------------------------


def _blend_fn(mode: str, cb: np.ndarray, cs: np.ndarray) -> np.ndarray:
    if mode == "normal":
        return cs
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


def blend(bottom: np.ndarray, top: np.ndarray, mode: str = "normal", opacity: float = 1.0) -> np.ndarray:
    ab = bottom[..., 3:4]
    as_ = top[..., 3:4] * opacity
    cb, cs = bottom[..., :3], np.clip(top[..., :3], 0, 1)
    mixed = (1 - ab) * cs + ab * _blend_fn(mode, cb, cs)
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
                 W: int, H: int) -> np.ndarray:
    """Il livello da solo, posato su una tela trasparente del documento, maschera e stili inclusi."""
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
        canvas = _paint_image(layer, W, H, s)
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


def composite(project: Project, base_dir: str | None = None, scale: float = 1.0,
              upto: int | None = None, transparent: bool = False, skip: set[str] | None = None) -> np.ndarray:
    """Composito dei livelli ``[0, upto)`` come array float. ``skip``: id da saltare."""
    W = max(1, int(round(project.width * scale)))
    H = max(1, int(round(project.height * scale)))
    out = _sfondo(project, W, H, transparent)
    layers = project.layers if upto is None else project.layers[:upto]
    for layer in layers:
        if not layer.visible or (skip and layer.id in skip):
            continue
        if layer.type == "adjustment":
            adj = out.copy()
            for e in layer.effects:
                if e.enabled:
                    adj = fx.apply(adj, e.type, e.params, scale=scale)
            m = _mask_array(layer, project, base_dir, scale, W, H, None, None)
            k = layer.opacity * (m[..., None] if m is not None else 1.0)
            adj[..., 3] = out[..., 3]
            if layer.blend == "normal":
                out = out * (1 - k) + adj * k
            else:
                top = adj.copy()
                top[..., 3] = (k if np.ndim(k) == 0 else k[..., 0]) * out[..., 3]
                out = blend(out, top, layer.blend, 1.0)
            continue
        top = layer_canvas(layer, project, base_dir, scale, W, H)
        out = blend(out, top, layer.blend, layer.opacity)
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
    i = project.index_of(layer_id)
    layer = project.layers[i]
    W = max(1, int(round(project.width * scale)))
    H = max(1, int(round(project.height * scale)))
    if layer.type == "adjustment":
        return render(project, base_dir, scale, upto=i + 1)
    return fx.to_pil(layer_canvas(layer, project, base_dir, scale, W, H))


# --------------------------------------------------------------------------
# esportazione
# --------------------------------------------------------------------------

FORMATI = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP", ".tif": "TIFF",
           ".tiff": "TIFF", ".bmp": "BMP", ".gif": "GIF"}


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
    if fmt in ("JPEG", "WEBP"):
        kw["quality"] = int(quality)
    if fmt == "JPEG":
        kw["optimize"] = True
    if dpi and fmt in ("JPEG", "PNG", "TIFF"):
        kw["dpi"] = (dpi, dpi)
    tmp = p.with_name(f"~{p.stem}.tmp{p.suffix}")
    out.save(tmp, fmt, **kw)
    os.replace(tmp, p)
    return p
