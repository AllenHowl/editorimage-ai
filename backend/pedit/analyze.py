"""图片测量和文档检查。

盲目调亮已经过曝的照片会让结果更差。``stats`` 提供曝光、对比度、
溢出像素、偏色和清晰度等数值；``suggest_enhance`` 将其转换为普通滤镜，
``review`` 检查完整文档并报告问题。
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from . import effects as fx
from . import render
from .model import Project


def stats(arr: np.ndarray) -> dict:
    """``arr`` 为 0..1 范围的浮点 RGBA 数组，仅统计可见像素。"""
    vis = arr[..., 3] > 0.5
    if not vis.any():
        return {"empty": True}
    rgb = arr[..., :3][vis]
    y = fx.luminance(rgb)
    mean = rgb.mean(axis=0)
    _, s, _ = fx._rgb_hsv(rgb[None, ...])
    # 清晰度：缩小采样后计算亮度拉普拉斯算子的方差。
    yl = fx.luminance(arr[..., :3])
    lap = (np.roll(yl, 1, 0) + np.roll(yl, -1, 0) + np.roll(yl, 1, 1) + np.roll(yl, -1, 1) - 4 * yl)[1:-1, 1:-1]
    q = np.clip(rgb * 255, 0, 255).astype(np.uint8)
    # 主色：粗略量化后统计出现次数。
    bins = (q // 32).astype(np.int32)
    key = bins[:, 0] * 64 + bins[:, 1] * 8 + bins[:, 2]
    counts = np.bincount(key, minlength=512)
    top = np.argsort(counts)[::-1][:5]
    dominanti = []
    for k in top:
        if counts[k] == 0:
            break
        sel = key == k
        c = q[sel].mean(axis=0).astype(int)
        dominanti.append({"color": "#%02x%02x%02x" % tuple(c), "share": round(float(counts[k] / len(key)), 3)})
    cast = mean - mean.mean()
    return {
        "brightness": round(float(y.mean()), 3),
        "contrast": round(float(y.std()), 3),
        "shadows_clipped": round(float((y < 0.02).mean()), 4),
        "highlights_clipped": round(float((y > 0.98).mean()), 4),
        "saturation": round(float(s.mean()), 3),
        "color_cast": {"r": round(float(cast[0]), 3), "g": round(float(cast[1]), 3), "b": round(float(cast[2]), 3)},
        "sharpness": round(float(lap.var() * 1000), 3),
        "percentiles": {p: round(float(np.percentile(y, p)), 3) for p in (1, 50, 99)},
        "dominant_colors": dominanti,
    }


def channel_stats(arr: np.ndarray) -> tuple[list[float], list[float]]:
    vis = arr[..., 3] > 0.5
    rgb = arr[..., :3][vis] if vis.any() else arr[..., :3].reshape(-1, 3)
    return rgb.mean(axis=0).tolist(), rgb.std(axis=0).tolist()


def suggest_enhance(arr: np.ndarray, strength: float = 1.0) -> list[dict]:
    """根据测量结果生成色阶、白平衡、对比度和自然饱和度修正滤镜。

    返回带可读参数的普通效果，用户或智能体可逐项调整。
    """
    vis = arr[..., 3] > 0.5
    if not vis.any():
        return []
    rgb = arr[..., :3][vis]
    y = fx.luminance(rgb)
    out: list[dict] = []
    lo, hi = np.percentile(y, 0.5), np.percentile(y, 99.5)
    black = float(np.clip(lo * 255 * strength, 0, 60))
    white = float(np.clip(255 - (1 - hi) * 255 * strength, 190, 255))
    mid = float(np.median(y))
    # 拉伸色阶后，用伽马将亮度中位数调整到约 0.45。
    stretched = np.clip((mid - black / 255) / max((white - black) / 255, 1e-3), 0.02, 0.98)
    gamma = float(np.clip(np.log(stretched) / np.log(0.45), 0.6, 1.8)) if strength > 0 else 1.0
    gamma = 1 + (gamma - 1) * strength
    if black > 2 or white < 253 or abs(gamma - 1) > 0.03:
        out.append({"type": "levels", "params": {"black": round(black, 1), "white": round(white, 1),
                                                 "gamma": round(gamma, 3)}})
    # 偏色：使用灰世界假设，仅修正超过阈值的部分。
    mean = rgb.mean(axis=0)
    warm = float(mean[0] - mean[2])
    if abs(warm) > 0.03:
        out.append({"type": "temperature", "params": {"amount": round(float(np.clip(-warm * 3 * strength, -1, 1)), 3)}})
    green = float(mean[1] - (mean[0] + mean[2]) / 2)
    if abs(green) > 0.025:
        out.append({"type": "tint", "params": {"amount": round(float(np.clip(green * 4 * strength, -1, 1)), 3)}})
    _, s, _ = fx._rgb_hsv(rgb[None, ...])
    sat = float(s.mean())
    if sat < 0.35:
        out.append({"type": "vibrance", "params": {"amount": round(float((0.35 - sat) * 1.5 * strength), 3)}})
    if float(y.std()) < 0.17:
        out.append({"type": "contrast", "params": {"amount": round(float((0.17 - y.std()) * 2 * strength), 3)}})
    return out


def review(project: Project, base_dir: str | None = None) -> list[dict]:
    """检查文档问题，并为每项问题提供可用的修复工具。"""
    avvisi: list[dict] = []
    if not project.layers:
        return [{"issue": "文档为空", "fix": "add_image / add_text / generate_image"}]
    W, H = project.width, project.height
    for ly in project.walk():
        nome = ly.name or ly.id
        if not ly.visible:
            continue
        if ly.type in ("image", "text", "shape"):
            try:
                w, h = render.content_size(ly, project, base_dir)
            except (KeyError, FileNotFoundError) as exc:
                avvisi.append({"layer": ly.id, "issue": f"{nome}：内容缺失（{exc})", "fix": "remove_layer"})
                continue
            if ly.x >= W or ly.y >= H or ly.x + w <= 0 or ly.y + h <= 0:
                avvisi.append({"layer": ly.id, "issue": f"{nome} 完全超出画布",
                               "fix": "使用 set_transform 或 align_layer"})
            elif ly.type == "text" and (ly.x < 0 or ly.y < 0 or ly.x + w > W or ly.y + h > H):
                avvisi.append({"layer": ly.id, "issue": f"文字 {nome} 被画布边缘裁切",
                               "fix": "使用带 margin 的 align_layer，或调整 set_text 的 size / max_width"})
            if ly.type == "image" and ly.scale > 1.5:
                avvisi.append({"layer": ly.id, "issue": f"{nome} 被放大了 {ly.scale:.1f} 倍，可能变得模糊",
                               "fix": "使用 upscale（AI 放大）增加像素"})
        if ly.type == "text" and ly.style and ly.style.size < min(W, H) * 0.015:
            avvisi.append({"layer": ly.id, "issue": f"文字 {nome} 相对画布过小",
                           "fix": "使用 set_text 调整 size"})
        if ly.opacity < 0.05:
            avvisi.append({"layer": ly.id, "issue": f"{nome} 几乎不可见（不透明度 {ly.opacity})",
                           "fix": "使用 set_layer 调整 opacity，或通过 remove_layer 删除图层"})
    # 检查文字与下方背景的对比度。
    for i, ly in enumerate(project.layers):
        if ly.type != "text" or not ly.visible or not ly.style:
            continue
        has_style = any(e.type in ("stroke", "drop_shadow", "outer_glow") and e.enabled for e in ly.effects)
        if has_style or ly.style.stroke_width > 0:
            continue
        try:
            w, h = render.content_size(ly, project, base_dir)
            s = min(1.0, 600 / max(W, 1))
            under = render.composite(project, base_dir, scale=s, upto=i)
            x0, y0 = int(max(0, ly.x * s)), int(max(0, ly.y * s))
            x1, y1 = int(min(W, ly.x + w) * s), int(min(H, ly.y + h) * s)
            if x1 <= x0 or y1 <= y0:
                continue
            bg = fx.luminance(under[y0:y1, x0:x1, :3]).mean()
            r, g, b, _ = fx.parse_color(ly.style.color)
            fg = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
            if abs(float(bg) - fg) < 0.3:
                avvisi.append({"layer": ly.id,
                               "issue": f"文字 {ly.name or ly.id} 与下方图片的对比度不足",
                               "fix": "修改颜色，或用 add_effect 添加 stroke / drop_shadow，或在文字下方添加形状"})
        except Exception:  # noqa: BLE001 - 单个异常图层不应导致整个检查失败。
            continue
    try:
        img = render.composite(project, base_dir, scale=min(1.0, 800 / max(W, 1)))
        st = stats(img)
        if not st.get("empty"):
            if st["highlights_clipped"] > 0.03:
                avvisi.append({"issue": f"{st['highlights_clipped']:.0%} 的图片区域高光过曝",
                               "fix": "使用 shadows_highlights 并设置 highlights>0，或设置 exposure<0"})
            if st["shadows_clipped"] > 0.08:
                avvisi.append({"issue": f"{st['shadows_clipped']:.0%} 的图片区域暗部丢失",
                               "fix": "使用 shadows_highlights 并设置 shadows>0，或调整 levels"})
    except Exception:  # noqa: BLE001
        pass
    return avvisi


def to_pil(arr: np.ndarray) -> Image.Image:
    return fx.to_pil(arr)
