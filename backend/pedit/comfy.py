"""本地 ComfyUI 的轻量客户端：生成、生成式填充和放大。

pedit 不内置大型扩散模型及 GPU 运行环境，而是通过 HTTP API 连接本机
ComfyUI（通常位于 ``127.0.0.1:8188``），自动从已安装模型中选择检查点。
未安装 ComfyUI 时，生成工具会说明连接方法，其余编辑功能仍可使用。

配置保存在 ``~/.pedit/comfy.json``，包含 ``url``、``checkpoint``、
``upscaler``、``steps``、``cfg``，也可使用 ``PEDIT_COMFY_URL``。
"""

from __future__ import annotations

import io
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from PIL import Image

DEFAULT_URL = "http://127.0.0.1:8188"


class ComfyError(RuntimeError):
    pass


def _cfg_file() -> Path:
    base = Path(os.environ.get("PEDIT_HOME") or (Path.home() / ".pedit"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "comfy.json"


def config() -> dict:
    try:
        cfg = json.loads(_cfg_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    cfg["url"] = (os.environ.get("PEDIT_COMFY_URL") or cfg.get("url") or DEFAULT_URL).rstrip("/")
    cfg.setdefault("steps", 25)
    cfg.setdefault("cfg", 7.0)
    return cfg


def set_config(**kw: Any) -> dict:
    cfg = config()
    for k, v in kw.items():
        if v is None:
            continue
        if v == "":
            cfg.pop(k, None)
        else:
            cfg[k] = v
    f = _cfg_file()
    tmp = f.with_name("~" + f.name)
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, f)
    return cfg


def _get(path: str, timeout: float = 10.0) -> Any:
    url = config()["url"] + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = r.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ComfyError(f"无法连接 ComfyUI：{config()['url']}: {exc}") from exc
    try:
        return json.loads(data)
    except ValueError:
        return data


def _post(path: str, body: bytes, ctype: str, timeout: float = 30.0) -> Any:
    url = config()["url"] + path
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        dettaglio = exc.read().decode("utf-8", "replace")[:600]
        raise ComfyError(f"ComfyUI 拒绝了任务（{exc.code}): {dettaglio}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ComfyError(f"无法连接 ComfyUI：{config()['url']}: {exc}") from exc


def available(timeout: float = 1.5) -> bool:
    try:
        _get("/system_stats", timeout=timeout)
        return True
    except ComfyError:
        return False


def _choices(node: str, field: str) -> list[str]:
    info = _get(f"/object_info/{node}")
    try:
        spec = info[node]["input"]["required"][field]
    except (KeyError, TypeError):
        return []
    first = spec[0] if spec else []
    if isinstance(first, list):
        return first
    # 新格式：["COMBO", {"options": [...]}]。
    if len(spec) > 1 and isinstance(spec[1], dict):
        return list(spec[1].get("options") or [])
    return []


def checkpoints() -> list[str]:
    return _choices("CheckpointLoaderSimple", "ckpt_name")


def upscalers() -> list[str]:
    try:
        return _choices("UpscaleModelLoader", "model_name")
    except ComfyError:
        return []


def status() -> dict:
    cfg = config()
    if not available():
        return {"ok": False, "url": cfg["url"],
                "hint": "请启动 ComfyUI（默认 127.0.0.1:8188），或使用 comfy_settings 设置地址"}
    ck = checkpoints()
    return {"ok": True, "url": cfg["url"], "checkpoints": ck, "checkpoint": pick_checkpoint(ck, cfg),
            "upscalers": upscalers()}


def pick_checkpoint(ck: list[str] | None = None, cfg: dict | None = None, inpaint: bool = False) -> str:
    cfg = cfg or config()
    if ck is None:
        ck = checkpoints()
    if not ck:
        raise ComfyError("ComfyUI 的 models/checkpoints 目录中没有模型，请下载 SD 1.5 或 SDXL 等检查点")
    voluto = cfg.get("inpaint_checkpoint" if inpaint else "checkpoint") or cfg.get("checkpoint")
    if voluto and voluto in ck:
        return voluto
    if inpaint:
        dedicati = [c for c in ck if "inpaint" in c.lower()]
        if dedicati:
            return dedicati[0]
    return ck[0]


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------


def upload(img: Image.Image, name: str = "pedit") -> str:
    """上传 PNG 到 ComfyUI 的 input 目录，返回供 LoadImage 使用的文件名。"""
    buf = io.BytesIO()
    img.save(buf, "PNG")
    boundary = uuid.uuid4().hex
    fname = f"{name}_{uuid.uuid4().hex[:8]}.png"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{fname}\"\r\n"
        f"Content-Type: image/png\r\n\r\n").encode() + buf.getvalue() + (
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue"
        f"\r\n--{boundary}--\r\n").encode()
    res = _post("/upload/image", body, f"multipart/form-data; boundary={boundary}")
    return res.get("name", fname) if isinstance(res, dict) else fname


def run(workflow: dict, timeout: float = 900.0) -> list[Image.Image]:
    """提交 API 格式的工作流到队列，并返回保存的图片。"""
    client = uuid.uuid4().hex
    res = _post("/prompt", json.dumps({"prompt": workflow, "client_id": client}).encode(), "application/json")
    pid = res.get("prompt_id")
    if not pid:
        raise ComfyError(f"ComfyUI 未接受工作流：{res}")
    t0 = time.monotonic()
    while True:
        hist = _get(f"/history/{pid}")
        if isinstance(hist, dict) and pid in hist:
            item = hist[pid]
            st = item.get("status") or {}
            if st.get("status_str") == "error":
                raise ComfyError(f"ComfyUI 任务失败：{json.dumps(st.get('messages'))[:600]}")
            immagini = []
            for out in (item.get("outputs") or {}).values():
                for im in out.get("images") or []:
                    q = urllib.parse.urlencode({"filename": im["filename"], "subfolder": im.get("subfolder", ""),
                                                "type": im.get("type", "output")})
                    data = _get(f"/view?{q}", timeout=60)
                    immagini.append(Image.open(io.BytesIO(data)).convert("RGBA"))
            if immagini:
                return immagini
            if st.get("completed"):
                raise ComfyError("ComfyUI 已完成，但没有生成图片")
        if time.monotonic() - t0 > timeout:
            raise ComfyError("ComfyUI 执行超时")
        time.sleep(0.6)


def _seed(seed: int | None) -> int:
    return int(seed) if seed is not None and seed >= 0 else random.randint(0, 2 ** 31 - 1)


def workflow_txt2img(prompt: str, negative: str, width: int, height: int, seed: int,
                     ckpt: str, steps: int, cfg: float) -> dict:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "5": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0],
            "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": "euler_ancestral",
            "scheduler": "normal", "denoise": 1.0}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "pedit"}},
    }


def workflow_inpaint(image_name: str, prompt: str, negative: str, seed: int, ckpt: str,
                     steps: int, cfg: float, denoise: float = 1.0) -> dict:
    """LoadImage 的 MASK 为 alpha 的反相：透明像素表示需要重绘。"""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "5": {"class_type": "VAEEncodeForInpaint", "inputs": {
            "pixels": ["4", 0], "vae": ["1", 2], "mask": ["4", 1], "grow_mask_by": 8}},
        "6": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["5", 0],
            "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": "euler_ancestral",
            "scheduler": "normal", "denoise": denoise}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": "pedit_fill"}},
    }


def workflow_img2img(image_name: str, prompt: str, negative: str, seed: int, ckpt: str,
                     steps: int, cfg: float, denoise: float) -> dict:
    """图生图：将图片编码为潜变量，``denoise`` 控制修改程度。"""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["1", 1]}},
        "4": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "5": {"class_type": "VAEEncode", "inputs": {"pixels": ["4", 0], "vae": ["1", 2]}},
        "6": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["5", 0],
            "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": "euler_ancestral",
            "scheduler": "normal", "denoise": denoise}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": "pedit_edit"}},
    }


def workflow_upscale(image_name: str, model: str) -> dict:
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": model}},
        "3": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
        "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0], "filename_prefix": "pedit_up"}},
    }


NEGATIVE = "blurry, low quality, deformed, watermark, text, jpeg artifacts"


def generate(prompt: str, width: int = 768, height: int = 768, negative: str | None = None,
             seed: int | None = None) -> Image.Image:
    cfg = config()
    w, h = (max(64, int(v) // 8 * 8) for v in (width, height))
    wf = workflow_txt2img(prompt, negative or NEGATIVE, w, h, _seed(seed), pick_checkpoint(cfg=cfg),
                          int(cfg["steps"]), float(cfg["cfg"]))
    return run(wf)[0]


def inpaint(img: Image.Image, mask: Image.Image, prompt: str, negative: str | None = None,
            seed: int | None = None) -> Image.Image:
    """``img`` 为 RGB，``mask`` 为 L（255 表示重绘），尺寸相同且为 8 的倍数。"""
    cfg = config()
    rgba = img.convert("RGBA")
    alpha = Image.eval(mask.convert("L"), lambda v: 255 - v)
    rgba.putalpha(alpha)
    name = upload(rgba, "pedit_fill")
    wf = workflow_inpaint(name, prompt, negative or NEGATIVE, _seed(seed),
                          pick_checkpoint(cfg=cfg, inpaint=True), int(cfg["steps"]), float(cfg["cfg"]))
    return run(wf)[0].resize(img.size, Image.Resampling.LANCZOS)


def img2img(img: Image.Image, prompt: str, strength: float = 0.6, negative: str | None = None,
            seed: int | None = None) -> Image.Image:
    """``img`` 为 RGB，尺寸为 8 的倍数；``strength`` 范围为 0..1（0 保持原图，1 完全重绘）。"""
    cfg = config()
    name = upload(img.convert("RGB"), "pedit_edit")
    wf = workflow_img2img(name, prompt, negative or NEGATIVE, _seed(seed), pick_checkpoint(cfg=cfg),
                          int(cfg["steps"]), float(cfg["cfg"]), float(min(1.0, max(0.05, strength))))
    return run(wf)[0].resize(img.size, Image.Resampling.LANCZOS)


def upscale(img: Image.Image, model: str | None = None) -> Image.Image:
    cfg = config()
    modelli = upscalers()
    scelto = model or cfg.get("upscaler") or (modelli[0] if modelli else None)
    if not scelto:
        raise ComfyError("ComfyUI 的 models/upscale_models 中没有放大模型，例如 RealESRGAN_x4plus")
    name = upload(img.convert("RGB"), "pedit_up")
    return run(workflow_upscale(name, scelto))[0]
