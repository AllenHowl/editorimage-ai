"""Registro dei filtri e degli stili di livello.

Ogni voce ha parametri dichiarati (tipo, intervallo, default, descrizione) e
una funzione che lavora su un array numpy float32 ``(H, W, 4)`` in 0..1 con
alpha *non* premoltiplicato. La validazione sta qui, una volta sola: UI,
assistente e agente MCP passano tutti da ``validate``.

Due famiglie:
- ``filter``: cambiano i pixel del contenuto (colore, nitidezza, rumore...).
  Vengono applicati prima della maschera;
- ``style``: ombra, contorno, bagliore, sovrapposizione colore. Lavorano sulla
  sagoma gia' mascherata, come gli stili di livello di Photoshop.

I parametri marcati ``px`` sono lunghezze in pixel del documento: al render di
un'anteprima ridotta vengono scalati, cosi' un'ombra di 20 px resta grande
uguale in proporzione a qualunque ingrandimento.
"""

from __future__ import annotations

import math
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageColor, ImageFilter


class EffectError(ValueError):
    pass


# --------------------------------------------------------------------------
# utilita' numpy <-> PIL
# --------------------------------------------------------------------------


def to_np(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("RGBA"), dtype=np.float32) / 255.0


def to_pil(arr: np.ndarray) -> Image.Image:
    return Image.fromarray((np.clip(arr, 0, 1) * 255.0 + 0.5).astype(np.uint8), "RGBA")


def parse_color(value: str | None, default: str = "#000000") -> tuple[int, int, int, int]:
    """'#rgb', '#rrggbb', '#rrggbbaa', nomi CSS, 'transparent'."""
    v = (value or default).strip()
    if v.lower() in ("transparent", "none", ""):
        return (0, 0, 0, 0)
    try:
        c = ImageColor.getcolor(v, "RGBA")
    except ValueError as exc:
        raise EffectError(f"invalid color {value!r}: use #rrggbb or a CSS name") from exc
    return tuple(c)  # type: ignore[return-value]


def luminance(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722


def _blur_arr(arr: np.ndarray, radius: float) -> np.ndarray:
    """Sfocatura gaussiana di un canale o di un'immagine RGBA, via Pillow."""
    if radius <= 0:
        return arr
    if arr.ndim == 2:
        im = Image.fromarray((np.clip(arr, 0, 1) * 255).astype(np.uint8), "L")
        return np.asarray(im.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32) / 255.0
    # premoltiplicato: altrimenti il colore dei pixel trasparenti "sporca" i bordi
    a = arr[..., 3:4]
    pm = np.concatenate([arr[..., :3] * a, a], axis=-1)
    im = Image.fromarray((np.clip(pm, 0, 1) * 255).astype(np.uint8), "RGBA")
    out = np.asarray(im.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32) / 255.0
    a2 = out[..., 3:4]
    rgb = np.where(a2 > 1e-4, out[..., :3] / np.maximum(a2, 1e-4), 0)
    return np.concatenate([rgb, a2], axis=-1)


def _rgb_hsv(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx = np.max(rgb, axis=-1)
    mn = np.min(rgb, axis=-1)
    d = mx - mn
    h = np.zeros_like(mx)
    m = d > 1e-6
    rm = m & (mx == r)
    gm = m & (mx == g) & ~rm
    bm = m & ~rm & ~gm
    h[rm] = ((g - b)[rm] / d[rm]) % 6
    h[gm] = (b - r)[gm] / d[gm] + 2
    h[bm] = (r - g)[bm] / d[bm] + 4
    h = h / 6.0
    s = np.where(mx > 1e-6, d / np.maximum(mx, 1e-6), 0)
    return h, s, mx


def _hsv_rgb(h: np.ndarray, s: np.ndarray, v: np.ndarray) -> np.ndarray:
    i = np.floor(h * 6).astype(np.int32) % 6
    f = h * 6 - np.floor(h * 6)
    p = v * (1 - s)
    q = v * (1 - f * s)
    t = v * (1 - (1 - f) * s)
    out = np.zeros(h.shape + (3,), dtype=np.float32)
    for k, (a, b, c) in enumerate(((v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q))):
        sel = i == k
        out[sel, 0], out[sel, 1], out[sel, 2] = a[sel], b[sel], c[sel]
    return out


def _with_rgb(arr: np.ndarray, rgb: np.ndarray) -> np.ndarray:
    return np.concatenate([np.clip(rgb, 0, 1), arr[..., 3:4]], axis=-1)


def _noise(shape: tuple[int, ...], seed: int) -> np.ndarray:
    """Rumore deterministico: lo stesso documento deve dare gli stessi pixel."""
    return np.random.default_rng(seed).standard_normal(shape).astype(np.float32)


# --------------------------------------------------------------------------
# filtri
# --------------------------------------------------------------------------


def f_brightness(a, p, ctx):
    return _with_rgb(a, a[..., :3] + p["amount"] * 0.5)


def f_contrast(a, p, ctx):
    k = math.tan((p["amount"] + 1) * math.pi / 4)   # -1..1 -> 0..inf, 0 -> 1
    return _with_rgb(a, (a[..., :3] - 0.5) * k + 0.5)


def f_exposure(a, p, ctx):
    return _with_rgb(a, a[..., :3] * (2.0 ** p["stops"]))


def f_gamma(a, p, ctx):
    return _with_rgb(a, np.power(np.clip(a[..., :3], 0, 1), 1.0 / p["gamma"]))


def f_saturation(a, p, ctx):
    rgb = a[..., :3]
    y = luminance(rgb)[..., None]
    return _with_rgb(a, y + (rgb - y) * (1.0 + p["amount"]))


def f_vibrance(a, p, ctx):
    """Satura di piu' i colori spenti, quasi niente quelli gia' carichi (pelle salva)."""
    rgb = a[..., :3]
    _, s, _ = _rgb_hsv(rgb)
    y = luminance(rgb)[..., None]
    k = 1.0 + p["amount"] * (1.0 - s)[..., None]
    return _with_rgb(a, y + (rgb - y) * k)


def f_temperature(a, p, ctx):
    """Positivo = piu' caldo (rosso su, blu giu')."""
    t = p["amount"] * 0.15
    rgb = a[..., :3] * np.array([1 + t, 1.0, 1 - t], dtype=np.float32)
    return _with_rgb(a, rgb)


def f_tint(a, p, ctx):
    """Positivo = verso il magenta, negativo = verso il verde."""
    t = p["amount"] * 0.12
    rgb = a[..., :3] * np.array([1 + t / 2, 1 - t, 1 + t / 2], dtype=np.float32)
    return _with_rgb(a, rgb)


def f_hue(a, p, ctx):
    h, s, v = _rgb_hsv(np.clip(a[..., :3], 0, 1))
    h = (h + p["degrees"] / 360.0) % 1.0
    return _with_rgb(a, _hsv_rgb(h, s, v))


def f_levels(a, p, ctx):
    lo, hi = p["black"] / 255.0, p["white"] / 255.0
    if hi <= lo:
        raise EffectError("levels: white must be greater than black")
    x = np.clip((a[..., :3] - lo) / (hi - lo), 0, 1)
    x = np.power(x, 1.0 / p["gamma"])
    olo, ohi = p["out_black"] / 255.0, p["out_white"] / 255.0
    return _with_rgb(a, olo + x * (ohi - olo))


def _curve_lut(points: list[list[float]]) -> np.ndarray:
    pts = sorted((float(x), float(y)) for x, y in points)
    xs = np.array([x for x, _ in pts]) / 255.0
    ys = np.array([y for _, y in pts]) / 255.0
    grid = np.linspace(0, 1, 256)
    return np.interp(grid, xs, ys).astype(np.float32)


def f_curves(a, p, ctx):
    out = a[..., :3].copy()
    idx = lambda c: np.clip(c * 255 + 0.5, 0, 255).astype(np.int32)  # noqa: E731
    if p.get("rgb"):
        lut = _curve_lut(p["rgb"])
        out = lut[idx(out)]
    for i, ch in enumerate(("r", "g", "b")):
        if p.get(ch):
            lut = _curve_lut(p[ch])
            out[..., i] = lut[idx(out[..., i])]
    return _with_rgb(a, out)


def f_shadows_highlights(a, p, ctx):
    rgb = a[..., :3]
    y = luminance(rgb)
    # maschere morbide: ombre sotto 0.5, luci sopra
    sh = np.clip(1 - y * 2, 0, 1) ** 2
    hl = np.clip(y * 2 - 1, 0, 1) ** 2
    delta = p["shadows"] * 0.5 * sh - p["highlights"] * 0.5 * hl
    return _with_rgb(a, rgb + delta[..., None])


def f_invert(a, p, ctx):
    return _with_rgb(a, 1.0 - a[..., :3])


def f_grayscale(a, p, ctx):
    y = luminance(a[..., :3])[..., None]
    return _with_rgb(a, a[..., :3] * (1 - p["amount"]) + y * p["amount"])


def f_sepia(a, p, ctx):
    m = np.array([[0.393, 0.769, 0.189], [0.349, 0.686, 0.168], [0.272, 0.534, 0.131]], dtype=np.float32)
    sep = a[..., :3] @ m.T
    return _with_rgb(a, a[..., :3] * (1 - p["amount"]) + sep * p["amount"])


def f_posterize(a, p, ctx):
    n = int(p["levels"]) - 1
    return _with_rgb(a, np.round(a[..., :3] * n) / n)


def f_threshold(a, p, ctx):
    y = luminance(a[..., :3])
    v = (y >= p["level"] / 255.0).astype(np.float32)[..., None]
    return _with_rgb(a, np.repeat(v, 3, axis=-1))


def f_auto_contrast(a, p, ctx):
    """Stira ogni canale fra i percentili: toglie la velatura e le dominanti."""
    rgb = a[..., :3]
    sel = a[..., 3] > 0.5
    if not sel.any():
        return a
    out = np.empty_like(rgb)
    cut = p["clip"]
    for i in range(3):
        ch = rgb[..., i][sel]
        lo, hi = np.percentile(ch, cut), np.percentile(ch, 100 - cut)
        out[..., i] = (rgb[..., i] - lo) / max(hi - lo, 1e-3)
    return _with_rgb(a, out)


def f_color_match(a, p, ctx):
    """Trasferimento di Reinhard per canale: media e deviazione verso un riferimento.

    I parametri li calcola ``Store.match_color`` misurando i due livelli: qui
    si applicano e basta, cosi' il render resta una funzione pura del documento.
    """
    rgb = a[..., :3]
    src_m, src_s = np.array(p["src_mean"], np.float32), np.array(p["src_std"], np.float32)
    dst_m, dst_s = np.array(p["dst_mean"], np.float32), np.array(p["dst_std"], np.float32)
    moved = (rgb - src_m) * (dst_s / np.maximum(src_s, 1e-3)) + dst_m
    return _with_rgb(a, rgb + (moved - rgb) * p["amount"])


def f_blur(a, p, ctx):
    return _blur_arr(a, p["radius"] * ctx["scale"])


def f_motion_blur(a, p, ctx):
    dist = max(1, int(round(p["distance"] * ctx["scale"])))
    ang = math.radians(p["angle"])
    dx, dy = math.cos(ang), -math.sin(ang)
    acc = np.zeros_like(a)
    n = min(dist, 48)
    for i in range(n):
        t = (i / max(n - 1, 1) - 0.5) * dist
        acc += np.roll(a, (int(round(t * dy)), int(round(t * dx))), axis=(0, 1))
    return acc / n


def f_sharpen(a, p, ctx):
    r = max(0.3, p["radius"] * ctx["scale"])
    b = _blur_arr(a, r)
    return _with_rgb(a, a[..., :3] + (a[..., :3] - b[..., :3]) * p["amount"])


def f_noise(a, p, ctx):
    h, w = a.shape[:2]
    if p["mono"]:
        n = _noise((h, w, 1), int(p["seed"]))
    else:
        n = _noise((h, w, 3), int(p["seed"]))
    return _with_rgb(a, a[..., :3] + n * p["amount"] * 0.25)


def f_pixelate(a, p, ctx):
    size = max(1, int(round(p["size"] * ctx["scale"])))
    if size <= 1:
        return a
    h, w = a.shape[:2]
    im = to_pil(a)
    small = im.resize((max(1, w // size), max(1, h // size)), Image.Resampling.BOX)
    return to_np(small.resize((w, h), Image.Resampling.NEAREST))


def f_vignette(a, p, ctx):
    h, w = a.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    nx, ny = (xx / max(w - 1, 1) - 0.5) * 2, (yy / max(h - 1, 1) - 0.5) * 2
    d = np.sqrt(nx ** 2 + ny ** 2) / math.sqrt(2)
    inner = 1 - p["softness"]
    k = np.clip((d - inner * 0.7) / max(1 - inner * 0.7, 1e-3), 0, 1) ** 2
    return _with_rgb(a, a[..., :3] * (1 - k * p["amount"])[..., None])


def f_denoise(a, p, ctx):
    size = int(p["size"])
    size += 1 - size % 2
    im = to_pil(a)
    rgb = im.convert("RGB").filter(ImageFilter.MedianFilter(size))
    out = to_np(rgb.convert("RGBA"))
    out[..., 3] = a[..., 3]
    return out


def f_emboss(a, p, ctx):
    im = to_pil(a).convert("RGB").filter(ImageFilter.EMBOSS)
    out = to_np(im.convert("RGBA"))
    out[..., 3] = a[..., 3]
    return out


def f_edges(a, p, ctx):
    im = to_pil(a).convert("RGB").filter(ImageFilter.FIND_EDGES)
    out = to_np(im.convert("RGBA"))
    out[..., 3] = a[..., 3]
    return out


def f_chromatic_aberration(a, p, ctx):
    k = int(round(p["amount"] * ctx["scale"]))
    if k == 0:
        return a
    out = a.copy()
    out[..., 0] = np.roll(a[..., 0], k, axis=1)
    out[..., 2] = np.roll(a[..., 2], -k, axis=1)
    return out


# --------------------------------------------------------------------------
# stili di livello (sulla sagoma mascherata)
# --------------------------------------------------------------------------


def _over(top: np.ndarray, bottom: np.ndarray) -> np.ndarray:
    ta, ba = top[..., 3:4], bottom[..., 3:4]
    oa = ta + ba * (1 - ta)
    rgb = (top[..., :3] * ta + bottom[..., :3] * ba * (1 - ta)) / np.maximum(oa, 1e-6)
    return np.concatenate([rgb, oa], axis=-1)


def _solid(shape: tuple[int, int], color: str, alpha: np.ndarray) -> np.ndarray:
    r, g, b, ca = parse_color(color)
    out = np.empty(shape + (4,), dtype=np.float32)
    out[..., 0], out[..., 1], out[..., 2] = r / 255, g / 255, b / 255
    out[..., 3] = alpha * (ca / 255)
    return out


def s_drop_shadow(a, p, ctx):
    s = ctx["scale"]
    alpha = a[..., 3]
    dx, dy = int(round(p["dx"] * s)), int(round(p["dy"] * s))
    sh = np.zeros_like(alpha)
    h, w = alpha.shape
    ys, xs = slice(max(dy, 0), h + min(dy, 0)), slice(max(dx, 0), w + min(dx, 0))
    ys2, xs2 = slice(max(-dy, 0), h + min(-dy, 0)), slice(max(-dx, 0), w + min(-dx, 0))
    sh[ys, xs] = alpha[ys2, xs2]
    sh = _blur_arr(sh, p["blur"] * s) * p["opacity"]
    return _over(a, _solid(alpha.shape, p["color"], sh))


def s_outer_glow(a, p, ctx):
    alpha = a[..., 3]
    g = _blur_arr(alpha, p["radius"] * ctx["scale"])
    g = np.clip(g * 2.0, 0, 1) * p["opacity"]
    return _over(a, _solid(alpha.shape, p["color"], g))


def s_stroke(a, p, ctx):
    """Contorno esterno: dilatazione dell'alpha con MaxFilter."""
    width = max(1, int(round(p["width"] * ctx["scale"])))
    alpha = a[..., 3]
    im = Image.fromarray((alpha * 255).astype(np.uint8), "L")
    size = width * 2 + 1
    # MaxFilter accetta solo dimensioni dispari e piccole: si ripete
    while size > 1:
        step = min(size, 9)
        step -= 1 - step % 2
        im = im.filter(ImageFilter.MaxFilter(step))
        size -= step - 1
    dil = np.asarray(im, dtype=np.float32) / 255.0
    return _over(a, _solid(alpha.shape, p["color"], dil * p["opacity"]))


def s_color_overlay(a, p, ctx):
    r, g, b, _ = parse_color(p["color"])
    rgb = np.array([r, g, b], dtype=np.float32) / 255
    out = a.copy()
    out[..., :3] = a[..., :3] * (1 - p["opacity"]) + rgb * p["opacity"]
    return out


# --------------------------------------------------------------------------
# registro
# --------------------------------------------------------------------------


def N(default: float, lo: float, hi: float, desc: str, px: bool = False) -> dict:
    return {"type": "number", "default": default, "min": lo, "max": hi, "desc": desc, "px": px}


def I(default: int, lo: int, hi: int, desc: str) -> dict:  # noqa: E743
    return {"type": "int", "default": default, "min": lo, "max": hi, "desc": desc}


def C(default: str, desc: str = "color") -> dict:
    return {"type": "color", "default": default, "desc": desc}


def B(default: bool, desc: str) -> dict:
    return {"type": "bool", "default": default, "desc": desc}


def P(desc: str) -> dict:
    return {"type": "points", "default": None, "desc": desc}


def V(desc: str) -> dict:
    return {"type": "vector", "default": [0.0, 0.0, 0.0], "desc": desc}


REGISTRY: dict[str, dict[str, Any]] = {
    # colore e tono
    "brightness": {"kind": "filter", "group": "color", "fn": f_brightness, "desc": "Brighten or darken",
                   "params": {"amount": N(0.0, -1, 1, "-1 darker .. +1 brighter")}},
    "contrast": {"kind": "filter", "group": "color", "fn": f_contrast, "desc": "Contrast around mid grey",
                 "params": {"amount": N(0.0, -0.95, 0.95, "-1 flat .. +1 punchy")}},
    "exposure": {"kind": "filter", "group": "color", "fn": f_exposure, "desc": "Exposure in stops",
                 "params": {"stops": N(0.0, -5, 5, "+1 = twice the light")}},
    "gamma": {"kind": "filter", "group": "color", "fn": f_gamma, "desc": "Midtone gamma",
              "params": {"gamma": N(1.0, 0.1, 5, ">1 brighter midtones")}},
    "saturation": {"kind": "filter", "group": "color", "fn": f_saturation, "desc": "Color intensity",
                   "params": {"amount": N(0.0, -1, 2, "-1 grey .. +1 double")}},
    "vibrance": {"kind": "filter", "group": "color", "fn": f_vibrance,
                 "desc": "Boost muted colors, protect already saturated ones (skin)",
                 "params": {"amount": N(0.0, -1, 2, "strength")}},
    "temperature": {"kind": "filter", "group": "color", "fn": f_temperature, "desc": "White balance warm/cool",
                    "params": {"amount": N(0.0, -1, 1, "+ warmer, - cooler")}},
    "tint": {"kind": "filter", "group": "color", "fn": f_tint, "desc": "White balance green/magenta",
             "params": {"amount": N(0.0, -1, 1, "+ magenta, - green")}},
    "hue": {"kind": "filter", "group": "color", "fn": f_hue, "desc": "Rotate hue",
            "params": {"degrees": N(0.0, -180, 180, "degrees")}},
    "levels": {"kind": "filter", "group": "color", "fn": f_levels, "desc": "Input/output levels with gamma",
               "params": {"black": N(0, 0, 254, "input black point 0-255"),
                          "white": N(255, 1, 255, "input white point 0-255"),
                          "gamma": N(1.0, 0.1, 5, "midtones"),
                          "out_black": N(0, 0, 255, "output black"),
                          "out_white": N(255, 0, 255, "output white")}},
    "curves": {"kind": "filter", "group": "color", "fn": f_curves,
               "desc": "Tone curves: lists of [in, out] points 0-255 per channel",
               "params": {"rgb": P("master curve [[0,0],[128,140],[255,255]]"),
                          "r": P("red curve"), "g": P("green curve"), "b": P("blue curve")}},
    "shadows_highlights": {"kind": "filter", "group": "color", "fn": f_shadows_highlights,
                           "desc": "Lift shadows / recover highlights",
                           "params": {"shadows": N(0.0, -1, 1, "+ lifts shadows"),
                                      "highlights": N(0.0, -1, 1, "+ pulls highlights down")}},
    "auto_contrast": {"kind": "filter", "group": "color", "fn": f_auto_contrast,
                      "desc": "Stretch each channel between percentiles (removes haze and casts)",
                      "params": {"clip": N(0.5, 0, 10, "percent clipped at each end")}},
    "color_match": {"kind": "filter", "group": "color", "fn": f_color_match,
                    "desc": "Color transfer computed by match_color (do not set by hand)",
                    "params": {"src_mean": V("source mean rgb"), "src_std": V("source std rgb"),
                               "dst_mean": V("target mean rgb"), "dst_std": V("target std rgb"),
                               "amount": N(1.0, 0, 1, "strength")}},
    "grayscale": {"kind": "filter", "group": "color", "fn": f_grayscale, "desc": "Black and white",
                  "params": {"amount": N(1.0, 0, 1, "strength")}},
    "sepia": {"kind": "filter", "group": "color", "fn": f_sepia, "desc": "Sepia toning",
              "params": {"amount": N(1.0, 0, 1, "strength")}},
    "invert": {"kind": "filter", "group": "color", "fn": f_invert, "desc": "Negative", "params": {}},
    "posterize": {"kind": "filter", "group": "stylize", "fn": f_posterize, "desc": "Reduce tones per channel",
                  "params": {"levels": I(6, 2, 64, "tones per channel")}},
    "threshold": {"kind": "filter", "group": "stylize", "fn": f_threshold, "desc": "Pure black and white",
                  "params": {"level": N(128, 0, 255, "cut point")}},
    # dettaglio
    "blur": {"kind": "filter", "group": "detail", "fn": f_blur, "desc": "Gaussian blur",
             "params": {"radius": N(4.0, 0, 200, "radius in px", px=True)}},
    "motion_blur": {"kind": "filter", "group": "detail", "fn": f_motion_blur, "desc": "Directional blur",
                    "params": {"distance": N(20.0, 1, 400, "length in px", px=True),
                               "angle": N(0.0, -180, 180, "degrees, 0 = horizontal")}},
    "sharpen": {"kind": "filter", "group": "detail", "fn": f_sharpen, "desc": "Unsharp mask",
                "params": {"amount": N(0.8, 0, 5, "strength"),
                           "radius": N(1.5, 0.3, 20, "radius in px", px=True)}},
    "denoise": {"kind": "filter", "group": "detail", "fn": f_denoise, "desc": "Median noise reduction",
                "params": {"size": I(3, 3, 9, "window (odd)")}},
    "noise": {"kind": "filter", "group": "detail", "fn": f_noise, "desc": "Film grain / noise",
              "params": {"amount": N(0.2, 0, 1, "strength"), "mono": B(True, "same noise on all channels"),
                         "seed": I(1, 0, 99999, "pattern")}},
    # stilizzazione
    "pixelate": {"kind": "filter", "group": "stylize", "fn": f_pixelate, "desc": "Mosaic",
                 "params": {"size": N(16, 1, 500, "block size in px", px=True)}},
    "vignette": {"kind": "filter", "group": "stylize", "fn": f_vignette, "desc": "Darken the corners",
                 "params": {"amount": N(0.5, 0, 1, "strength"), "softness": N(0.5, 0, 1, "falloff")}},
    "emboss": {"kind": "filter", "group": "stylize", "fn": f_emboss, "desc": "Relief", "params": {}},
    "edges": {"kind": "filter", "group": "stylize", "fn": f_edges, "desc": "Find edges", "params": {}},
    "chromatic_aberration": {"kind": "filter", "group": "stylize", "fn": f_chromatic_aberration,
                             "desc": "Red/blue fringe",
                             "params": {"amount": N(4, 0, 60, "shift in px", px=True)}},
    # stili di livello
    "drop_shadow": {"kind": "style", "group": "style", "fn": s_drop_shadow, "desc": "Drop shadow",
                    "params": {"dx": N(8, -500, 500, "offset x px", px=True),
                               "dy": N(8, -500, 500, "offset y px", px=True),
                               "blur": N(10, 0, 200, "softness px", px=True),
                               "color": C("#000000"), "opacity": N(0.6, 0, 1, "opacity")}},
    "outer_glow": {"kind": "style", "group": "style", "fn": s_outer_glow, "desc": "Glow around the shape",
                   "params": {"radius": N(16, 1, 300, "size px", px=True), "color": C("#ffffff"),
                              "opacity": N(0.8, 0, 1, "opacity")}},
    "stroke": {"kind": "style", "group": "style", "fn": s_stroke, "desc": "Outline around the shape",
               "params": {"width": N(4, 1, 100, "width px", px=True), "color": C("#000000"),
                          "opacity": N(1.0, 0, 1, "opacity")}},
    "color_overlay": {"kind": "style", "group": "style", "fn": s_color_overlay, "desc": "Fill the shape with a color",
                      "params": {"color": C("#ff0000"), "opacity": N(1.0, 0, 1, "opacity")}},
}


def spec(name: str) -> dict:
    if name not in REGISTRY:
        raise EffectError(f"unknown effect {name!r}; available: {', '.join(REGISTRY)}")
    return REGISTRY[name]


def _num(name: str, key: str, v: Any, sp: dict) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError) as exc:
        raise EffectError(f"{name}.{key}: expected a number, got {v!r}") from exc
    if not math.isfinite(x) or x < sp["min"] or x > sp["max"]:
        raise EffectError(f"{name}.{key}={v} out of range [{sp['min']}, {sp['max']}]")
    return x


def validate(name: str, params: dict | None, partial: bool = False) -> dict:
    """Parametri completi e controllati. ``partial`` per gli aggiornamenti."""
    sp = spec(name)
    params = dict(params or {})
    unknown = set(params) - set(sp["params"])
    if unknown:
        raise EffectError(f"{name}: unknown parameters {sorted(unknown)}; "
                          f"accepted: {sorted(sp['params']) or 'none'}")
    out: dict[str, Any] = {}
    for key, ps in sp["params"].items():
        if key not in params:
            if not partial:
                out[key] = ps["default"]
            continue
        v = params[key]
        t = ps["type"]
        if t == "number":
            out[key] = _num(name, key, v, ps)
        elif t == "int":
            out[key] = int(round(_num(name, key, v, ps)))
        elif t == "bool":
            out[key] = bool(v)
        elif t == "color":
            parse_color(v)
            out[key] = str(v)
        elif t == "points":
            if v is None:
                out[key] = None
                continue
            if not isinstance(v, list) or len(v) < 2 or not all(
                    isinstance(q, (list, tuple)) and len(q) == 2 for q in v):
                raise EffectError(f"{name}.{key}: expected a list of [in, out] pairs, at least two")
            out[key] = [[float(q[0]), float(q[1])] for q in v]
        elif t == "vector":
            if not isinstance(v, (list, tuple)) or len(v) != 3:
                raise EffectError(f"{name}.{key}: expected [r, g, b]")
            out[key] = [float(q) for q in v]
    return out


def apply(arr: np.ndarray, name: str, params: dict, scale: float = 1.0) -> np.ndarray:
    sp = spec(name)
    full = {**validate(name, {}), **params}
    return sp["fn"](arr, full, {"scale": scale}).astype(np.float32)


def describe() -> list[dict]:
    """Catalogo per UI, assistente e ``list_effects``."""
    out = []
    for name, sp in REGISTRY.items():
        out.append({"name": name, "kind": sp["kind"], "group": sp["group"], "desc": sp["desc"],
                    "params": {k: {kk: vv for kk, vv in v.items()} for k, v in sp["params"].items()}})
    return out


Fn = Callable[[np.ndarray, dict, dict], np.ndarray]
