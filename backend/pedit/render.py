"""将文档转换为像素。

渲染是文档的纯函数，无隐藏状态，噪点使用固定种子。界面预览、智能体
查看的图片和导出文件均由此生成，确保所见即所得。

每个图层先绘制到与文档同尺寸的透明画布，再依次执行：
滤镜 → 蒙版 → 样式 → 不透明度 → 与下方内容混合。
调整图层对下方合成结果应用滤镜，再通过自身蒙版重新混合。

``scale`` 用于预览：缩小渲染尺寸时，同步缩放模糊、阴影、描边及文字等
以像素为单位的参数。
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
# 磁盘素材（带缓存）。
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
    """读取 RGBA 图片并应用 EXIF 旋转，按文件修改时间缓存。"""
    p = Path(path)
    try:
        st = p.stat()
    except OSError as exc:
        raise FileNotFoundError(f"未找到图片：{p}") from exc
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
# 字体
# --------------------------------------------------------------------------

_FAMIGLIE = {
    "sans": ["arial", "segoeui", "helvetica", "DejaVuSans", "LiberationSans-Regular", "Roboto-Regular"],
    "serif": ["times", "georgia", "DejaVuSerif", "LiberationSerif-Regular"],
    "mono": ["consola", "cour", "DejaVuSansMono", "LiberationMono-Regular"],
    "display": ["impact", "Anton-Regular", "BebasNeue-Regular", "arialbd", "DejaVuSans-Bold"],
    "handwriting": ["comic", "segoepr", "DejaVuSans"],
}
_VARIANTI = {
    # 将（粗体、斜体）映射到 Windows 和 Linux 常见字体文件后缀。
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
    """按字体族（如 'sans'、'serif'）、文件名（如 'arial'）或路径查找字体。"""
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
# 已按目标比例缩放的图层内容。
# --------------------------------------------------------------------------


def _text_image(layer: Layer, k: float) -> tuple[Image.Image, tuple[float, float]]:
    """直接按最终尺寸绘制文字，避免缩放造成锯齿。"""
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
    """返回文字在图层缩放后占据的文档像素范围。"""
    _, (w, h) = _text_image(layer, 1.0)
    return w, h


def _shape_image(layer: Layer, k: float) -> tuple[Image.Image, tuple[float, float]]:
    sh = layer.shape
    sw = sh.stroke_width * k if sh.stroke else 0.0
    pad = int(math.ceil(sw / 2)) + 1
    w, h = max(1, int(round(sh.w * k))), max(1, int(round(sh.h * k)))
    # 先以 4 倍尺寸绘制再缩小，平滑边缘，不依赖 ImageDraw 抗锯齿。
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
    # 描边留白对称分布，将图片中心与形状中心对齐后，
    # 形状会准确落在 x、y 处。
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
    """返回已乘以 ``scale`` 的内容尺寸，单位为文档像素。"""
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
        dx, dy = math.sin(ang), -math.cos(ang)    # 90 表示从左到右，180 表示从上到下。
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
    """计算笔画覆盖率（0..1），包含硬度和不透明度。"""
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
    """``out[y, x] = arr[y + dy, x + dx]``，画布外为透明。"""
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
    # 低频信息可接受 8 位精度，使用 Pillow 即可。
    im = Image.fromarray((np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8), "RGB")
    return np.asarray(im.filter(ImageFilter.GaussianBlur(r)), np.float32) / 255


def _clone_top(st, sotto: np.ndarray, a: np.ndarray, s: float) -> np.ndarray:
    """仿制图章像素：将 ``sotto`` 按 ``clone`` 偏移取样；heal 模式匹配目标区域光线。"""
    dx, dy = (int(round(v * s)) for v in st.clone)
    src = _sposta(sotto, dx, dy)
    if st.heal:
        # 低频来自目标区域，细节来自取样源。
        r = max(1.0, st.size * s / 3)
        rgb = np.clip(src[..., :3] + _sfoca(sotto[..., :3], r) - _sfoca(src[..., :3], r), 0, 1)
        src = np.concatenate([rgb, src[..., 3:4]], axis=-1)
    top = src.copy()
    top[..., 3] = a * src[..., 3]
    return top


def _paint_image(layer: Layer, W: int, H: int, s: float, sotto: np.ndarray | None = None) -> Image.Image:
    """按顺序绘制笔画；``erase`` 降低已绘制内容的 alpha。

    ``clone`` 笔画从 ``sotto``（图层下方合成内容）取样；
    未提供 ``sotto`` 时不绘制。
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
# 定位和蒙版
# --------------------------------------------------------------------------


def _trasforma(img: Image.Image, layer: Layer, native: tuple[float, float], s: float,
               W: int, H: int, mode: str = "RGBA") -> Image.Image:
    """翻转、旋转并将已缩放的 ``img`` 放到文档尺寸的画布上。"""
    if layer.flip_h:
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if layer.flip_v:
        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    if layer.rotation % 360:
        # 正值为顺时针，与 Photoshop 一致；PIL 正值为逆时针。
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
        # RGBA 蒙版含透明度时使用 alpha，否则使用亮度。
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
            # 在蒙版上擦除等同于涂白，使内容重新可见。
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
# 混合（W3C 合成规则，alpha 不预乘）。
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
    """W3C SetSat：最大值设为 ``s``，最小值设为 0，中间值按比例缩放。"""
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
    raise ValueError(f"未知混合模式 {mode!r}")


def _dissolvi(alpha: np.ndarray) -> np.ndarray:
    """溶解：像素以 alpha 为概率完全显示或隐藏，固定种子保证渲染确定性。"""
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
# 渲染
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
    """将单个图层放到文档尺寸的透明画布上，包含蒙版和样式。

    ``sotto`` 为图层下方的合成内容，供仿制图章取样。
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
        # 隔离组：先在透明背景上合成子图层，再将整个组作为单层处理。
        canvas = fx.to_pil(_componi(layer.children, project, base_dir, s, np.zeros((H, W, 4), np.float32)))
    else:
        raise ValueError(f"无法单独绘制 {layer.type} 类型的图层")
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
    """对 ``out`` 应用调整图层滤镜，再按蒙版和不透明度混合。"""
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
    """穿透组：子图层直接作用于 ``out``，组蒙版和不透明度控制最终强度。"""
    dopo = _componi(layer.children, project, base_dir, s, out)
    m = _mask_array(layer, project, base_dir, s, W, H, None, None)
    k = layer.opacity * (m[..., None] if m is not None else 1.0)
    if np.ndim(k) == 0 and k >= 1:
        return dopo
    return out * (1 - k) + dopo * k


def _componi(layers: list[Layer], project: Project, base_dir: str | None, s: float,
             out: np.ndarray) -> np.ndarray:
    """从下到上将图层列表合成到 ``out``。

    剪贴蒙版由基底及紧邻其上方的 ``clip`` 图层组成，先在基底 alpha
    范围内合成，再按基底的混合模式和不透明度混合，规则与 Photoshop 一致。
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
            # 隐藏基底时，同时隐藏其整组剪贴图层。
            i = j
            continue
        if base.type == "adjustment":
            # 调整图层不能作为剪贴基底，上方的 clip 图层按普通图层处理。
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
    """将图层范围 ``[0, upto)`` 合成为浮点数组，跳过 ``skip`` 中的 ID。"""
    W = max(1, int(round(project.width * scale)))
    H = max(1, int(round(project.height * scale)))
    out = _sfondo(project, W, H, transparent)
    layers = project.layers if upto is None else project.layers[:upto]
    if skip:
        layers = [ly for ly in layers if ly.id not in skip]
    return _componi(layers, project, base_dir, scale, out)


def composite_below(project: Project, layer_id: str, base_dir: str | None = None, scale: float = 1.0,
                    transparent: bool = False) -> np.ndarray:
    """获取某图层下方可见的合成结果，也支持组内图层。

    隔离组从透明背景开始；穿透组从组下方的合成内容开始。
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
    """生成宽度为 ``width`` 像素的预览，不超过原图尺寸。"""
    s = min(1.0, width / max(project.width, 1))
    return render(project, base_dir, scale=s, **kw)


def layer_image(project: Project, layer_id: str, base_dir: str | None = None,
                scale: float = 1.0) -> Image.Image:
    """单独预览透明背景上的图层；调整图层显示其对下方内容的影响。"""
    layer = project.layer(layer_id)
    W = max(1, int(round(project.width * scale)))
    H = max(1, int(round(project.height * scale)))
    if layer.type == "adjustment":
        sotto = composite_below(project, layer.id, base_dir, scale)
        return fx.to_pil(_regola(sotto, layer, project, base_dir, scale, W, H))
    sotto = composite_below(project, layer.id, base_dir, scale) if has_clone(layer) else None
    return fx.to_pil(layer_canvas(layer, project, base_dir, scale, W, H, sotto=sotto))


# --------------------------------------------------------------------------
# 导出
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
        raise ValueError(f"不支持的格式 {p.suffix!r}；可使用 {', '.join(FORMATI)}")
    p.parent.mkdir(parents=True, exist_ok=True)
    out = img
    if fmt in ("JPEG", "BMP"):
        # 格式不支持 alpha 时，先合成到不透明背景上。
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
