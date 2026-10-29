"""滤镜和图层样式注册表。

每项声明参数类型、范围、默认值和说明，并提供处理 0..1 范围的 numpy
float32 ``(H, W, 4)`` 数组的函数，alpha 不预乘。界面、助手和 MCP 智能体
统一通过 ``validate`` 校验参数。

分为两类：
- ``filter``：改变内容像素，如颜色、清晰度和噪点，在蒙版之前应用。
- ``style``：阴影、描边、发光和颜色叠加，作用于蒙版处理后的轮廓，
  与 Photoshop 图层样式一致。

带 ``px`` 标记的参数以文档像素为单位，渲染缩略预览时按比例缩放，
确保 20 像素阴影在不同缩放比例下保持相对大小。
"""

from __future__ import annotations

import math
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageColor, ImageFilter


class EffectError(ValueError):
    pass


# --------------------------------------------------------------------------
# numpy 与 PIL 之间的转换工具。
# --------------------------------------------------------------------------


def to_np(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("RGBA"), dtype=np.float32) / 255.0


def to_pil(arr: np.ndarray) -> Image.Image:
    return Image.fromarray((np.clip(arr, 0, 1) * 255.0 + 0.5).astype(np.uint8), "RGBA")


def parse_color(value: str | None, default: str = "#000000") -> tuple[int, int, int, int]:
    """支持 '#rgb'、'#rrggbb'、'#rrggbbaa'、CSS 颜色名及 'transparent'。"""
    v = (value or default).strip()
    if v.lower() in ("transparent", "none", ""):
        return (0, 0, 0, 0)
    try:
        c = ImageColor.getcolor(v, "RGBA")
    except ValueError as exc:
        raise EffectError(f"无效颜色 {value!r}：请使用 #rrggbb 或 CSS 颜色名") from exc
    return tuple(c)  # type: ignore[return-value]


def luminance(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722


def _blur_arr(arr: np.ndarray, radius: float) -> np.ndarray:
    """通过 Pillow 对单通道或 RGBA 图片进行高斯模糊。"""
    if radius <= 0:
        return arr
    if arr.ndim == 2:
        im = Image.fromarray((np.clip(arr, 0, 1) * 255).astype(np.uint8), "L")
        return np.asarray(im.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32) / 255.0
    # 先预乘 alpha，避免透明像素的颜色污染边缘。
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
    """使用确定性噪点，保证相同文档始终生成相同像素。"""
    return np.random.default_rng(seed).standard_normal(shape).astype(np.float32)


# --------------------------------------------------------------------------
# 滤镜
# --------------------------------------------------------------------------


def f_brightness(a, p, ctx):
    return _with_rgb(a, a[..., :3] + p["amount"] * 0.5)


def f_contrast(a, p, ctx):
    k = math.tan((p["amount"] + 1) * math.pi / 4)   # 将 -1..1 映射到 0..正无穷，0 对应 1。
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
    """优先提高低饱和度颜色，尽量保留高饱和度颜色以保护肤色。"""
    rgb = a[..., :3]
    _, s, _ = _rgb_hsv(rgb)
    y = luminance(rgb)[..., None]
    k = 1.0 + p["amount"] * (1.0 - s)[..., None]
    return _with_rgb(a, y + (rgb - y) * k)


def f_temperature(a, p, ctx):
    """正值使色调变暖：增加红色，减少蓝色。"""
    t = p["amount"] * 0.15
    rgb = a[..., :3] * np.array([1 + t, 1.0, 1 - t], dtype=np.float32)
    return _with_rgb(a, rgb)


def f_tint(a, p, ctx):
    """正值偏洋红，负值偏绿色。"""
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
        raise EffectError("色阶：白场必须大于黑场")
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
    # 柔和蒙版：亮度小于 0.5 的部分为阴影，大于 0.5 的部分为高光。
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
    """按百分位拉伸各通道，去除灰雾和偏色。"""
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
    """按通道执行 Reinhard 颜色迁移，将均值和标准差调整到参考图。

    参数由 ``Store.match_color`` 测量两个图层后计算；此处仅应用参数，
    保持渲染为文档的纯函数。
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
# 图层样式（作用于蒙版后的轮廓）。
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
    """外部描边：通过 MaxFilter 膨胀 alpha 通道。"""
    width = max(1, int(round(p["width"] * ctx["scale"])))
    alpha = a[..., 3]
    im = Image.fromarray((alpha * 255).astype(np.uint8), "L")
    size = width * 2 + 1
    # MaxFilter 仅支持较小的奇数尺寸，因此重复应用。
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
# 注册表
# --------------------------------------------------------------------------


def N(default: float, lo: float, hi: float, desc: str, px: bool = False) -> dict:
    return {"type": "number", "default": default, "min": lo, "max": hi, "desc": desc, "px": px}


def I(default: int, lo: int, hi: int, desc: str) -> dict:  # noqa: E743
    return {"type": "int", "default": default, "min": lo, "max": hi, "desc": desc}


def C(default: str, desc: str = "颜色") -> dict:
    return {"type": "color", "default": default, "desc": desc}


def B(default: bool, desc: str) -> dict:
    return {"type": "bool", "default": default, "desc": desc}


def P(desc: str) -> dict:
    return {"type": "points", "default": None, "desc": desc}


def V(desc: str) -> dict:
    return {"type": "vector", "default": [0.0, 0.0, 0.0], "desc": desc}


REGISTRY: dict[str, dict[str, Any]] = {
    # 颜色和色调
    "brightness": {"label": "亮度", "kind": "filter", "group": "color", "fn": f_brightness, "desc": "提亮或压暗图片",
                   "params": {"amount": N(0.0, -1, 1, "-1 压暗，+1 提亮")}},
    "contrast": {"label": "对比度", "kind": "filter", "group": "color", "fn": f_contrast, "desc": "围绕中灰调整对比度",
                 "params": {"amount": N(0.0, -0.95, 0.95, "-1 柔和，+1 强烈")}},
    "exposure": {"label": "曝光", "kind": "filter", "group": "color", "fn": f_exposure, "desc": "按曝光档数调整亮度",
                 "params": {"stops": N(0.0, -5, 5, "+1 表示亮度加倍")}},
    "gamma": {"label": "伽马", "kind": "filter", "group": "color", "fn": f_gamma, "desc": "中间调伽马",
              "params": {"gamma": N(1.0, 0.1, 5, "大于 1 时提亮中间调")}},
    "saturation": {"label": "饱和度", "kind": "filter", "group": "color", "fn": f_saturation, "desc": "颜色强度",
                   "params": {"amount": N(0.0, -1, 2, "-1 灰度，+1 饱和度加倍")}},
    "vibrance": {"label": "自然饱和度", "kind": "filter", "group": "color", "fn": f_vibrance,
                 "desc": "增强低饱和度颜色，保护已饱和的颜色和肤色",
                 "params": {"amount": N(0.0, -1, 2, "强度")}},
    "temperature": {"label": "色温", "kind": "filter", "group": "color", "fn": f_temperature, "desc": "调整冷暖白平衡",
                    "params": {"amount": N(0.0, -1, 1, "正值偏暖，负值偏冷")}},
    "tint": {"label": "色调", "kind": "filter", "group": "color", "fn": f_tint, "desc": "调整绿色与洋红色之间的白平衡",
             "params": {"amount": N(0.0, -1, 1, "正值偏洋红，负值偏绿")}},
    "hue": {"label": "色相", "kind": "filter", "group": "color", "fn": f_hue, "desc": "旋转色相",
            "params": {"degrees": N(0.0, -180, 180, "角度")}},
    "levels": {"label": "色阶", "kind": "filter", "group": "color", "fn": f_levels, "desc": "输入和输出色阶及伽马调整",
               "params": {"black": N(0, 0, 254, "输入黑场，范围 0–255"),
                          "white": N(255, 1, 255, "输入白场，范围 0–255"),
                          "gamma": N(1.0, 0.1, 5, "中间调"),
                          "out_black": N(0, 0, 255, "输出黑场"),
                          "out_white": N(255, 0, 255, "输出白场")}},
    "curves": {"label": "曲线", "kind": "filter", "group": "color", "fn": f_curves,
               "desc": "色调曲线：每个通道使用 0–255 的 [输入, 输出] 控制点列表",
               "params": {"rgb": P("整体曲线，如 [[0,0],[128,140],[255,255]]"),
                          "r": P("红色曲线"), "g": P("绿色曲线"), "b": P("蓝色曲线")}},
    "shadows_highlights": {"label": "阴影 / 高光", "kind": "filter", "group": "color", "fn": f_shadows_highlights,
                           "desc": "提亮阴影，恢复高光",
                           "params": {"shadows": N(0.0, -1, 1, "正值提亮阴影"),
                                      "highlights": N(0.0, -1, 1, "正值压低高光")}},
    "auto_contrast": {"label": "自动对比度", "kind": "filter", "group": "color", "fn": f_auto_contrast,
                      "desc": "按百分位拉伸各通道，去除灰雾和偏色",
                      "params": {"clip": N(0.5, 0, 10, "两端各裁切的百分比")}},
    "color_match": {"label": "颜色匹配", "kind": "filter", "group": "color", "fn": f_color_match,
                    "desc": "由 match_color 计算颜色迁移参数，请勿手动设置",
                    "params": {"src_mean": V("源 RGB 均值"), "src_std": V("源 RGB 标准差"),
                               "dst_mean": V("目标 RGB 均值"), "dst_std": V("目标 RGB 标准差"),
                               "amount": N(1.0, 0, 1, "强度")}},
    "grayscale": {"label": "黑白", "kind": "filter", "group": "color", "fn": f_grayscale, "desc": "黑白效果",
                  "params": {"amount": N(1.0, 0, 1, "强度")}},
    "sepia": {"label": "褐色调", "kind": "filter", "group": "color", "fn": f_sepia, "desc": "褐色调效果",
              "params": {"amount": N(1.0, 0, 1, "强度")}},
    "invert": {"label": "反相", "kind": "filter", "group": "color", "fn": f_invert, "desc": "负片效果", "params": {}},
    "posterize": {"label": "色调分离", "kind": "filter", "group": "stylize", "fn": f_posterize, "desc": "减少各通道的色阶数量",
                  "params": {"levels": I(6, 2, 64, "每个通道的色阶数")}},
    "threshold": {"label": "阈值", "kind": "filter", "group": "stylize", "fn": f_threshold, "desc": "纯黑白效果",
                  "params": {"level": N(128, 0, 255, "分界阈值")}},
    # 细节
    "blur": {"label": "高斯模糊", "kind": "filter", "group": "detail", "fn": f_blur, "desc": "高斯模糊",
             "params": {"radius": N(4.0, 0, 200, "半径，单位为像素", px=True)}},
    "motion_blur": {"label": "动感模糊", "kind": "filter", "group": "detail", "fn": f_motion_blur, "desc": "方向性模糊",
                    "params": {"distance": N(20.0, 1, 400, "长度，单位为像素", px=True),
                               "angle": N(0.0, -180, 180, "角度，0 表示水平")}},
    "sharpen": {"label": "锐化", "kind": "filter", "group": "detail", "fn": f_sharpen, "desc": "反差保留锐化",
                "params": {"amount": N(0.8, 0, 5, "强度"),
                           "radius": N(1.5, 0.3, 20, "半径，单位为像素", px=True)}},
    "denoise": {"label": "降噪", "kind": "filter", "group": "detail", "fn": f_denoise, "desc": "中值滤波降噪",
                "params": {"size": I(3, 3, 9, "窗口大小（奇数）")}},
    "noise": {"label": "噪点", "kind": "filter", "group": "detail", "fn": f_noise, "desc": "胶片颗粒 / 噪点",
              "params": {"amount": N(0.2, 0, 1, "强度"), "mono": B(True, "所有通道使用相同噪点"),
                         "seed": I(1, 0, 99999, "噪点图案")}},
    # 风格化
    "pixelate": {"label": "马赛克", "kind": "filter", "group": "stylize", "fn": f_pixelate, "desc": "马赛克",
                 "params": {"size": N(16, 1, 500, "色块大小，单位为像素", px=True)}},
    "vignette": {"label": "暗角", "kind": "filter", "group": "stylize", "fn": f_vignette, "desc": "压暗四角",
                 "params": {"amount": N(0.5, 0, 1, "强度"), "softness": N(0.5, 0, 1, "衰减")}},
    "emboss": {"label": "浮雕", "kind": "filter", "group": "stylize", "fn": f_emboss, "desc": "浮雕", "params": {}},
    "edges": {"label": "查找边缘", "kind": "filter", "group": "stylize", "fn": f_edges, "desc": "查找边缘", "params": {}},
    "chromatic_aberration": {"label": "色差", "kind": "filter", "group": "stylize", "fn": f_chromatic_aberration,
                             "desc": "红蓝边缘色差",
                             "params": {"amount": N(4, 0, 60, "偏移，单位为像素", px=True)}},
    # 图层样式
    "drop_shadow": {"label": "投影", "kind": "style", "group": "style", "fn": s_drop_shadow, "desc": "投影",
                    "params": {"dx": N(8, -500, 500, "水平偏移，单位为像素", px=True),
                               "dy": N(8, -500, 500, "垂直偏移，单位为像素", px=True),
                               "blur": N(10, 0, 200, "柔和度，单位为像素", px=True),
                               "color": C("#000000"), "opacity": N(0.6, 0, 1, "不透明度")}},
    "outer_glow": {"label": "外发光", "kind": "style", "group": "style", "fn": s_outer_glow, "desc": "沿轮廓外部发光",
                   "params": {"radius": N(16, 1, 300, "大小，单位为像素", px=True), "color": C("#ffffff"),
                              "opacity": N(0.8, 0, 1, "不透明度")}},
    "stroke": {"label": "描边", "kind": "style", "group": "style", "fn": s_stroke, "desc": "沿形状轮廓描边",
               "params": {"width": N(4, 1, 100, "宽度，单位为像素", px=True), "color": C("#000000"),
                          "opacity": N(1.0, 0, 1, "不透明度")}},
    "color_overlay": {"label": "颜色叠加", "kind": "style", "group": "style", "fn": s_color_overlay, "desc": "用颜色填充形状",
                      "params": {"color": C("#ff0000"), "opacity": N(1.0, 0, 1, "不透明度")}},
}


def spec(name: str) -> dict:
    if name not in REGISTRY:
        raise EffectError(f"未知效果 {name!r}；可选：{', '.join(REGISTRY)}")
    return REGISTRY[name]


def _num(name: str, key: str, v: Any, sp: dict) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError) as exc:
        raise EffectError(f"{name}.{key}：需要数值，实际为 {v!r}") from exc
    if not math.isfinite(x) or x < sp["min"] or x > sp["max"]:
        raise EffectError(f"{name}.{key}={v} 超出范围 [{sp['min']}, {sp['max']}]")
    return x


def validate(name: str, params: dict | None, partial: bool = False) -> dict:
    """补全并校验参数，更新时可使用 ``partial`` 模式。"""
    sp = spec(name)
    params = dict(params or {})
    unknown = set(params) - set(sp["params"])
    if unknown:
        raise EffectError(f"{name}：未知参数 {sorted(unknown)}; "
                          f"可用参数：{sorted(sp['params']) or 'none'}")
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
                raise EffectError(f"{name}.{key}：需要至少两组 [输入, 输出] 控制点")
            out[key] = [[float(q[0]), float(q[1])] for q in v]
        elif t == "vector":
            if not isinstance(v, (list, tuple)) or len(v) != 3:
                raise EffectError(f"{name}.{key}：需要 [r, g, b]")
            out[key] = [float(q) for q in v]
    return out


def apply(arr: np.ndarray, name: str, params: dict, scale: float = 1.0) -> np.ndarray:
    sp = spec(name)
    full = {**validate(name, {}), **params}
    return sp["fn"](arr, full, {"scale": scale}).astype(np.float32)


def describe() -> list[dict]:
    """供界面、助手及 ``list_effects`` 使用的效果目录。"""
    out = []
    for name, sp in REGISTRY.items():
        out.append({"name": name, "kind": sp["kind"], "group": sp["group"], "label": sp["label"], "desc": sp["desc"],
                    "params": {k: {kk: vv for kk, vv in v.items()} for k, v in sp["params"].items()}})
    return out


Fn = Callable[[np.ndarray, dict, dict], np.ndarray]
