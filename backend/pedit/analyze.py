"""Misure sull'immagine e revisione del documento.

Un agente che non misura ritocca a caso: "un po' piu' luminosa" su una foto gia'
bruciata peggiora tutto. ``stats`` da' numeri che si leggono senza vedere
(esposizione, contrasto, pixel bruciati, dominante di colore, nitidezza),
``suggest_enhance`` li traduce in filtri normali, ``review`` guarda il
documento intero e dice cosa non va.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from . import effects as fx
from . import render
from .model import Project


def stats(arr: np.ndarray) -> dict:
    """``arr`` float RGBA 0..1. Solo i pixel visibili contano."""
    vis = arr[..., 3] > 0.5
    if not vis.any():
        return {"empty": True}
    rgb = arr[..., :3][vis]
    y = fx.luminance(rgb)
    mean = rgb.mean(axis=0)
    _, s, _ = fx._rgb_hsv(rgb[None, ...])
    # nitidezza: varianza del laplaciano sulla luminanza (su un campione ridotto)
    yl = fx.luminance(arr[..., :3])
    lap = (np.roll(yl, 1, 0) + np.roll(yl, -1, 0) + np.roll(yl, 1, 1) + np.roll(yl, -1, 1) - 4 * yl)[1:-1, 1:-1]
    q = np.clip(rgb * 255, 0, 255).astype(np.uint8)
    # colori dominanti: quantizzazione grossolana e conteggio
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
    """Filtri che correggono le misure: livelli, bilanciamento, contrasto, vivacita'.

    Non e' una scatola nera: escono effetti normali con valori leggibili, che
    l'utente o l'agente possono ritoccare uno per uno.
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
    # gamma che porta la mediana verso 0.45 (dopo lo stiramento dei livelli)
    stretched = np.clip((mid - black / 255) / max((white - black) / 255, 1e-3), 0.02, 0.98)
    gamma = float(np.clip(np.log(stretched) / np.log(0.45), 0.6, 1.8)) if strength > 0 else 1.0
    gamma = 1 + (gamma - 1) * strength
    if black > 2 or white < 253 or abs(gamma - 1) > 0.03:
        out.append({"type": "levels", "params": {"black": round(black, 1), "white": round(white, 1),
                                                 "gamma": round(gamma, 3)}})
    # dominante: mondo grigio, solo la parte che eccede una soglia
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
    """Problemi del documento, ognuno con lo strumento che lo sistema."""
    avvisi: list[dict] = []
    if not project.layers:
        return [{"issue": "empty document", "fix": "add_image / add_text / generate_image"}]
    W, H = project.width, project.height
    for ly in project.layers:
        nome = ly.name or ly.id
        if not ly.visible:
            continue
        if ly.type in ("image", "text", "shape"):
            try:
                w, h = render.content_size(ly, project, base_dir)
            except (KeyError, FileNotFoundError) as exc:
                avvisi.append({"layer": ly.id, "issue": f"{nome}: content missing ({exc})", "fix": "remove_layer"})
                continue
            if ly.x >= W or ly.y >= H or ly.x + w <= 0 or ly.y + h <= 0:
                avvisi.append({"layer": ly.id, "issue": f"{nome} is completely outside the canvas",
                               "fix": "set_transform or align_layer"})
            elif ly.type == "text" and (ly.x < 0 or ly.y < 0 or ly.x + w > W or ly.y + h > H):
                avvisi.append({"layer": ly.id, "issue": f"text {nome} is cut by the canvas edge",
                               "fix": "align_layer with a margin, or set_text size / max_width"})
            if ly.type == "image" and ly.scale > 1.5:
                avvisi.append({"layer": ly.id, "issue": f"{nome} is enlarged {ly.scale:.1f}x: it will look soft",
                               "fix": "upscale (AI) instead of scale"})
        if ly.type == "text" and ly.style and ly.style.size < min(W, H) * 0.015:
            avvisi.append({"layer": ly.id, "issue": f"text {nome} is very small for this canvas",
                           "fix": "set_text size"})
        if ly.opacity < 0.05:
            avvisi.append({"layer": ly.id, "issue": f"{nome} is almost invisible (opacity {ly.opacity})",
                           "fix": "set_layer opacity or remove_layer"})
    # contrasto dei testi con cio' che hanno sotto
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
                               "issue": f"text {ly.name or ly.id} has low contrast with the image under it",
                               "fix": "change color, or add_effect stroke / drop_shadow, or a shape behind it"})
        except Exception:  # noqa: BLE001 - una revisione non deve rompersi su un livello strano
            continue
    try:
        img = render.composite(project, base_dir, scale=min(1.0, 800 / max(W, 1)))
        st = stats(img)
        if not st.get("empty"):
            if st["highlights_clipped"] > 0.03:
                avvisi.append({"issue": f"{st['highlights_clipped']:.0%} of the image is blown out white",
                               "fix": "shadows_highlights highlights>0, or exposure < 0"})
            if st["shadows_clipped"] > 0.08:
                avvisi.append({"issue": f"{st['shadows_clipped']:.0%} of the image is crushed black",
                               "fix": "shadows_highlights shadows>0 or levels"})
    except Exception:  # noqa: BLE001
        pass
    return avvisi


def to_pil(arr: np.ndarray) -> Image.Image:
    return fx.to_pil(arr)
