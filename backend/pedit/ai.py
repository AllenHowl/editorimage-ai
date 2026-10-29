"""AI 功能：抠图、填充、生成和放大。

按计算开销提供以下能力及回退方案：
- 始终可用：基于 numpy 扩散的填充，适合天空、墙面、皮肤等平滑区域；
  Lanczos 插值放大并锐化。
- ``ai`` 扩展：rembg 抠图（首次使用时下载 ONNX 模型）和 OpenCV 传统填充。
- 本地 ComfyUI：按提示词生成式填充、文生图、图生图及模型放大（如 RealESRGAN）。
- 在线服务（``cloud``）：使用用户密钥调用 OpenAI、Gemini、Stability、
  OpenRouter、Replicate、Anthropic。``backend`` 可指定服务；``auto`` 仅在
  用户选择默认在线服务后使用，避免未经选择产生费用。

本模块仅处理图片；结果放到文档的哪个位置（新图层或蒙版）由 ``Store`` 决定。
"""

from __future__ import annotations

import importlib.util
import threading

import numpy as np
from PIL import Image, ImageFilter

from . import cloud, comfy


class AIError(RuntimeError):
    pass


def _ha(modulo: str) -> bool:
    return importlib.util.find_spec(modulo) is not None


def status(check_comfy: bool = True) -> dict:
    """以可读文字说明本机已具备和缺少的能力。"""
    st = {
        "remove_background": {"ok": _ha("rembg"),
                              "hint": "" if _ha("rembg") else "pip install \"pedit-mcp[ai]\""},
        "inpaint": {"ok": True, "backend": "opencv" if _ha("cv2") else "diffusion",
                    "hint": "" if _ha("cv2") else "安装 pip install \"pedit-mcp[ai]\" 可改善物体移除效果"},
    }
    if check_comfy:
        st["comfyui"] = comfy.status()
    st["online"] = cloud.riassunto()
    return st


LOCALI = ("auto", "comfyui", "opencv", "diffusion", "lanczos", "rembg", "local")


def _servizio(compito: str, backend: str) -> str | None:
    """返回要使用的在线服务 ID；本地 AI 返回 None。"""
    if backend in LOCALI and backend != "auto":
        return None
    if backend not in LOCALI and backend not in cloud.ids():
        raise AIError(f"未知后端 {backend!r}：可选 auto、comfyui、local 或在线 AI 服务 "
                      f"({', '.join(cloud.ids())})")
    try:
        return cloud.scegli(compito, backend)
    except cloud.CloudError as exc:
        raise AIError(str(exc)) from exc


def _in_rete(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except cloud.CloudError as exc:
        raise AIError(str(exc)) from exc


# --------------------------------------------------------------------------
# 抠图
# --------------------------------------------------------------------------

_SESSIONI: dict[str, object] = {}
_LOCK = threading.Lock()

MODELLI_SCONTORNO = ("u2net", "isnet-general-use", "u2net_human_seg", "birefnet-general", "silueta")


def subject_mask(img: Image.Image, model: str = "isnet-general-use", backend: str = "local") -> Image.Image:
    """返回主体的 L 模式蒙版（255 表示主体）。``backend`` 可选 local（rembg）或在线服务。"""
    if backend not in ("local", "rembg", "auto"):
        pid = _servizio("cutout", backend)
        return _in_rete(cloud.cutout, pid, img)
    if not _ha("rembg"):
        raise AIError("移除背景需要 AI 扩展：pip install \"pedit-mcp[ai]\"")
    if model not in MODELLI_SCONTORNO:
        raise AIError(f"未知模型 {model!r}；可选：{', '.join(MODELLI_SCONTORNO)}")
    import rembg

    with _LOCK:
        if model not in _SESSIONI:
            _SESSIONI[model] = rembg.new_session(model)
        sess = _SESSIONI[model]
    out = rembg.remove(img.convert("RGB"), session=sess, only_mask=True)
    return out.convert("L") if isinstance(out, Image.Image) else Image.fromarray(np.asarray(out)).convert("L")


# --------------------------------------------------------------------------
# 填充（移除物体）
# --------------------------------------------------------------------------


def _diffusione(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """使用图像金字塔和邻域均值填充蒙版覆盖的像素。

    在最粗层级，空洞仅有几个像素，可快速填补；更细层级以上一层结果为起点，
    通过少量迭代细化。适合无缝填充平滑背景，密集纹理中会留下柔和色块。
    """
    h, w = mask.shape
    if mask.sum() == 0:
        return rgb
    if min(h, w) <= 8:
        known = ~mask
        fill = rgb[known].mean(axis=0) if known.any() else np.full(3, 0.5, np.float32)
        out = rgb.copy()
        out[mask] = fill
        return out
    small_rgb = np.asarray(Image.fromarray((rgb * 255).astype(np.uint8)).resize(
        ((w + 1) // 2, (h + 1) // 2), Image.Resampling.BOX), np.float32) / 255
    small_m = np.asarray(Image.fromarray(mask.astype(np.uint8) * 255).resize(
        ((w + 1) // 2, (h + 1) // 2), Image.Resampling.BOX)) > 0
    coarse = _diffusione(small_rgb, small_m)
    up = np.asarray(Image.fromarray((np.clip(coarse, 0, 1) * 255).astype(np.uint8)).resize(
        (w, h), Image.Resampling.BILINEAR), np.float32) / 255
    out = rgb.copy()
    out[mask] = up[mask]
    for _ in range(12):
        avg = (np.roll(out, 1, 0) + np.roll(out, -1, 0) + np.roll(out, 1, 1) + np.roll(out, -1, 1)) / 4
        out[mask] = avg[mask]
    return out


def fill_classic(img: Image.Image, mask: Image.Image) -> Image.Image:
    """无提示词填充：优先使用 OpenCV Telea，否则使用扩散算法。"""
    rgb = img.convert("RGB")
    m = mask.convert("L")
    if _ha("cv2"):
        import cv2

        arr = np.asarray(rgb)[:, :, ::-1].copy()
        mm = (np.asarray(m) > 127).astype(np.uint8) * 255
        r = max(3, int(min(arr.shape[:2]) * 0.01))
        out = cv2.inpaint(arr, mm, r, cv2.INPAINT_TELEA)
        return Image.fromarray(out[:, :, ::-1].copy())
    arr = np.asarray(rgb, np.float32) / 255
    filled = _diffusione(arr, np.asarray(m) > 127)
    return Image.fromarray((np.clip(filled, 0, 1) * 255 + 0.5).astype(np.uint8))


def _multipli_8(img: Image.Image, mask: Image.Image, lato: int = 768) -> tuple[Image.Image, Image.Image]:
    w, h = img.size
    k = lato / max(w, h)
    nw, nh = max(64, int(w * k) // 8 * 8), max(64, int(h * k) // 8 * 8)
    return (img.resize((nw, nh), Image.Resampling.LANCZOS),
            mask.resize((nw, nh), Image.Resampling.BILINEAR))


def fill(img: Image.Image, mask: Image.Image, prompt: str | None = None, backend: str = "auto",
         seed: int | None = None) -> tuple[Image.Image, str]:
    """填充 ``mask`` 的白色区域，返回图片及使用的引擎。

    提供提示词或指定 ``backend="comfyui"`` 时使用 ComfyUI 生成式填充；
    否则使用传统物体移除算法。
    """
    pid = _servizio("inpaint", backend) if (prompt or backend not in LOCALI) else None
    if pid:
        return _in_rete(cloud.inpaint, pid, img.convert("RGB"), mask, prompt or "clean background, seamless",
                        seed=seed), pid
    if backend not in ("auto", "comfyui", "opencv", "diffusion"):
        raise AIError("backend 必须为 auto、comfyui、opencv、diffusion 或在线 AI 服务 ID")
    if backend == "comfyui" or (backend == "auto" and prompt):
        if not comfy.available():
            raise AIError("生成式填充需要在本地运行 ComfyUI "
                          f"({comfy.config()['url']}）或在线 AI 服务（在“AI 模型”中配置）；"
                          "不填写提示词时使用传统物体移除算法")
        small, small_m = _multipli_8(img.convert("RGB"), mask)
        out = comfy.inpaint(small, small_m, prompt or "clean background, seamless", seed=seed)
        return out.convert("RGB").resize(img.size, Image.Resampling.LANCZOS), "comfyui"
    if backend == "diffusion" or (backend == "opencv" and not _ha("cv2")):
        arr = np.asarray(img.convert("RGB"), np.float32) / 255
        filled = _diffusione(arr, np.asarray(mask.convert("L")) > 127)
        return Image.fromarray((np.clip(filled, 0, 1) * 255 + 0.5).astype(np.uint8)), "diffusion"
    return fill_classic(img, mask), "opencv" if _ha("cv2") else "diffusion"


# --------------------------------------------------------------------------
# 生成和放大
# --------------------------------------------------------------------------


def generate(prompt: str, width: int, height: int, negative: str | None = None,
             seed: int | None = None, backend: str = "auto") -> tuple[Image.Image, str]:
    pid = _servizio("generate", backend)
    if pid:
        return _in_rete(cloud.generate, pid, prompt, width, height, negative, seed), pid
    if not comfy.available():
        raise AIError(f"生成图片需要在本地运行 ComfyUI（{comfy.config()['url']}) "
                      "或在“AI 模型”设置中配置带密钥的在线 AI 服务")
    return comfy.generate(prompt, width, height, negative, seed), "comfyui"


def edit(img: Image.Image, prompt: str, strength: float = 0.6, seed: int | None = None,
         backend: str = "auto") -> tuple[Image.Image, str]:
    """图生图：按提示词修改 ``img``，保持原尺寸。"""
    pid = _servizio("edit", backend)
    if pid:
        return _in_rete(cloud.edit, pid, img, prompt, strength, seed), pid
    if not comfy.available():
        raise AIError(f"图生图需要在本地运行 ComfyUI（{comfy.config()['url']}) "
                      "或在“AI 模型”设置中配置带密钥的在线 AI 服务")
    small, _ = _multipli_8(img.convert("RGB"), Image.new("L", img.size, 0))
    out = comfy.img2img(small, prompt, strength, seed=seed)
    return out.convert("RGB").resize(img.size, Image.Resampling.LANCZOS), "comfyui"


def describe(img: Image.Image, question: str | None = None, backend: str = "auto") -> tuple[str, str]:
    """使用在线视觉模型或本机 Ollama 描述图片。"""
    pid = _servizio("describe", backend)
    if not pid:
        raise AIError("描述图片需要视觉模型：请在“AI 模型”设置中添加密钥（OpenAI、Gemini、Anthropic、"
                      "OpenRouter），或配置 Ollama")
    return _in_rete(cloud.describe, pid, img, question), pid


def upscale(img: Image.Image, factor: float, backend: str = "auto") -> tuple[Image.Image, str]:
    w, h = img.size
    target = (max(1, int(round(w * factor))), max(1, int(round(h * factor))))
    pid = _servizio("upscale", backend) if backend not in ("lanczos",) else None
    if pid:
        up = _in_rete(cloud.upscale, pid, img.convert("RGB")).resize(target, Image.Resampling.LANCZOS)
        if img.mode == "RGBA":
            up = up.convert("RGBA")
            up.putalpha(img.getchannel("A").resize(target, Image.Resampling.LANCZOS))
        return up, pid
    if backend in ("auto", "comfyui") and comfy.available() and comfy.upscalers():
        rgb = img.convert("RGB")
        up = comfy.upscale(rgb).resize(target, Image.Resampling.LANCZOS)
        if img.mode == "RGBA":
            up = up.convert("RGBA")
            up.putalpha(img.getchannel("A").resize(target, Image.Resampling.LANCZOS))
        return up, "comfyui"
    if backend == "comfyui":
        raise AIError("未找到可用的 ComfyUI 放大模型")
    up = img.resize(target, Image.Resampling.LANCZOS)
    up = up.filter(ImageFilter.UnsharpMask(radius=max(1.0, factor * 0.8), percent=60, threshold=2))
    return up, "lanczos"
