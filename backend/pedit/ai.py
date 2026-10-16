"""Funzioni IA: scontorno, riempimento, generazione, upscale.

Tre livelli, dal piu' leggero al piu' pesante, e ognuno ha un ripiego:

- **sempre**: riempimento per diffusione in numpy (zone uniformi: cielo, muri,
  pelle), upscale Lanczos con nitidezza;
- **extra ``ai``**: scontorno con rembg (modelli onnx, scaricati al primo uso)
  e riempimento classico di OpenCV;
- **ComfyUI** locale: riempimento generativo con prompt, generazione da testo,
  image-to-image, upscale con un modello (RealESRGAN e simili);
- **servizi in rete** con una chiave (``cloud``): OpenAI, Gemini, Stability,
  OpenRouter, Replicate, Anthropic. ``backend`` puo' nominarne uno; con
  ``auto`` si usano solo se l'utente ne ha scelto uno come predefinito, cosi'
  niente costa soldi senza che lo si sia deciso.

Qui si lavora su immagini e basta: decidere *dove* mettere il risultato nel
documento (un livello nuovo, una maschera) e' compito di ``Store``.
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
    """Cosa c'e' su questa macchina e cosa manca, in parole."""
    st = {
        "remove_background": {"ok": _ha("rembg"),
                              "hint": "" if _ha("rembg") else "pip install \"pedit-mcp[ai]\""},
        "inpaint": {"ok": True, "backend": "opencv" if _ha("cv2") else "diffusion",
                    "hint": "" if _ha("cv2") else "pip install \"pedit-mcp[ai]\" for better object removal"},
    }
    if check_comfy:
        st["comfyui"] = comfy.status()
    st["online"] = cloud.riassunto()
    return st


LOCALI = ("auto", "comfyui", "opencv", "diffusion", "lanczos", "rembg", "local")


def _servizio(compito: str, backend: str) -> str | None:
    """Id del servizio in rete da usare, o None per l'IA locale."""
    if backend in LOCALI and backend != "auto":
        return None
    if backend not in LOCALI and backend not in cloud.ids():
        raise AIError(f"unknown backend {backend!r}: auto, comfyui, local, or an AI service "
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
# scontorno
# --------------------------------------------------------------------------

_SESSIONI: dict[str, object] = {}
_LOCK = threading.Lock()

MODELLI_SCONTORNO = ("u2net", "isnet-general-use", "u2net_human_seg", "birefnet-general", "silueta")


def subject_mask(img: Image.Image, model: str = "isnet-general-use", backend: str = "local") -> Image.Image:
    """Maschera L del soggetto principale (255 = soggetto). ``backend``: local (rembg) o un servizio."""
    if backend not in ("local", "rembg", "auto"):
        pid = _servizio("cutout", backend)
        return _in_rete(cloud.cutout, pid, img)
    if not _ha("rembg"):
        raise AIError("background removal needs the ai extra: pip install \"pedit-mcp[ai]\"")
    if model not in MODELLI_SCONTORNO:
        raise AIError(f"unknown model {model!r}; available: {', '.join(MODELLI_SCONTORNO)}")
    import rembg

    with _LOCK:
        if model not in _SESSIONI:
            _SESSIONI[model] = rembg.new_session(model)
        sess = _SESSIONI[model]
    out = rembg.remove(img.convert("RGB"), session=sess, only_mask=True)
    return out.convert("L") if isinstance(out, Image.Image) else Image.fromarray(np.asarray(out)).convert("L")


# --------------------------------------------------------------------------
# riempimento (rimozione oggetti)
# --------------------------------------------------------------------------


def _diffusione(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Riempie i pixel mascherati con la media dei vicini, a piramide.

    Al livello piu' grossolano il buco e' pochi pixel e si chiude subito; a
    ogni livello piu' fine si parte dalla soluzione di quello sotto e si
    rifinisce con qualche iterazione. Liscio e senza cuciture su fondi
    uniformi; su texture fitte lascia una macchia morbida.
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
    """Riempimento senza prompt: OpenCV Telea se c'e', altrimenti diffusione."""
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
    """Riempie la zona bianca di ``mask``. Ritorna l'immagine e il motore usato.

    Con un prompt (o ``backend="comfyui"``) il riempimento e' generativo e
    vuole ComfyUI; senza, e' la rimozione classica.
    """
    pid = _servizio("inpaint", backend) if (prompt or backend not in LOCALI) else None
    if pid:
        return _in_rete(cloud.inpaint, pid, img.convert("RGB"), mask, prompt or "clean background, seamless",
                        seed=seed), pid
    if backend not in ("auto", "comfyui", "opencv", "diffusion"):
        raise AIError("backend must be auto, comfyui, opencv, diffusion or an AI service")
    if backend == "comfyui" or (backend == "auto" and prompt):
        if not comfy.available():
            raise AIError("generative fill needs ComfyUI running locally "
                          f"({comfy.config()['url']}) or an online AI service (AI models settings); "
                          "without a prompt, fill uses the classic remover")
        small, small_m = _multipli_8(img.convert("RGB"), mask)
        out = comfy.inpaint(small, small_m, prompt or "clean background, seamless", seed=seed)
        return out.convert("RGB").resize(img.size, Image.Resampling.LANCZOS), "comfyui"
    if backend == "diffusion" or (backend == "opencv" and not _ha("cv2")):
        arr = np.asarray(img.convert("RGB"), np.float32) / 255
        filled = _diffusione(arr, np.asarray(mask.convert("L")) > 127)
        return Image.fromarray((np.clip(filled, 0, 1) * 255 + 0.5).astype(np.uint8)), "diffusion"
    return fill_classic(img, mask), "opencv" if _ha("cv2") else "diffusion"


# --------------------------------------------------------------------------
# generazione e upscale
# --------------------------------------------------------------------------


def generate(prompt: str, width: int, height: int, negative: str | None = None,
             seed: int | None = None, backend: str = "auto") -> tuple[Image.Image, str]:
    pid = _servizio("generate", backend)
    if pid:
        return _in_rete(cloud.generate, pid, prompt, width, height, negative, seed), pid
    if not comfy.available():
        raise AIError(f"image generation needs ComfyUI running locally ({comfy.config()['url']}) "
                      "or an online AI service with a key (AI models settings)")
    return comfy.generate(prompt, width, height, negative, seed), "comfyui"


def edit(img: Image.Image, prompt: str, strength: float = 0.6, seed: int | None = None,
         backend: str = "auto") -> tuple[Image.Image, str]:
    """Image-to-image: ``img`` cambiata secondo il prompt, stessa misura."""
    pid = _servizio("edit", backend)
    if pid:
        return _in_rete(cloud.edit, pid, img, prompt, strength, seed), pid
    if not comfy.available():
        raise AIError(f"image-to-image needs ComfyUI running locally ({comfy.config()['url']}) "
                      "or an online AI service with a key (AI models settings)")
    small, _ = _multipli_8(img.convert("RGB"), Image.new("L", img.size, 0))
    out = comfy.img2img(small, prompt, strength, seed=seed)
    return out.convert("RGB").resize(img.size, Image.Resampling.LANCZOS), "comfyui"


def describe(img: Image.Image, question: str | None = None, backend: str = "auto") -> tuple[str, str]:
    """Image-to-text con un modello di visione (in rete, o Ollama sul computer)."""
    pid = _servizio("describe", backend)
    if not pid:
        raise AIError("describing an image needs a vision model: paste a key (OpenAI, Gemini, Anthropic, "
                      "OpenRouter) or set up Ollama in AI models settings")
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
        raise AIError("ComfyUI with an upscale model is not available")
    up = img.resize(target, Image.Resampling.LANCZOS)
    up = up.filter(ImageFilter.UnsharpMask(radius=max(1.0, factor * 0.8), percent=60, threshold=2))
    return up, "lanczos"
