"""使用编辑器用户的密钥调用图片模型 API。

在本地 AI（rembg、OpenCV、ComfyUI）之外，支持在线文生图、图生图、
蒙版填充、图片描述、放大和抠图。每个服务在 ``fa`` 中声明支持的任务
及各任务的默认模型，用户可在设置中修改。

- ``openai``：通过 ``/images/generations`` 和 ``/images/edits`` 调用 GPT Image，
  通过 ``/chat/completions`` 调用视觉模型；``custom`` 支持任意兼容该协议的服务。
- ``gemini``：通过 ``generateContent`` 调用 Google Gemini 图片模型（Nano Banana）。
- ``stability``：Stability AI v2beta 的生成、填充、放大和移除背景接口。
- ``openrouter``：使用 ``modalities: ["image"]`` 通过聊天接口调用图片模型。
- ``replicate``：支持 ``owner/model`` 形式的模型，如 FLUX。
- ``anthropic``：使用 Claude 描述图片。
- ``ollama``：无需密钥的本机视觉模型。

密钥保存在 ``~/.pedit/ai_api.json``，不会写入文档。缺少时使用同 ID 的
助手配置（``llm.json``）或环境变量，一份密钥可供聊天和图片功能共用。

本模块仅与服务通信，输入和输出均为 PIL 图片；结果在文档中的位置由
``Store`` 决定。所有 HTTP 请求均通过 ``_http``，测试可替换它以避免访问网络。
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from PIL import Image

COMPITI = ("generate", "edit", "inpaint", "describe", "upscale", "cutout")

PROVIDERS: list[dict] = [
    {"id": "openai", "nome": "OpenAI (GPT Image)", "tipo": "openai", "chiave": True, "env": "OPENAI_API_KEY",
     "base": "https://api.openai.com/v1",
     "fa": {"generate": "gpt-image-1", "edit": "gpt-image-1", "inpaint": "gpt-image-1", "describe": "gpt-5-mini"},
     "suggeriti": ["gpt-image-1", "gpt-image-1-mini", "dall-e-3", "gpt-5", "gpt-5-mini"],
     "nota": "从 platform.openai.com 获取密钥，可与助手共用。"},
    {"id": "gemini", "nome": "Google Gemini (Nano Banana)", "tipo": "gemini", "chiave": True, "env": "GEMINI_API_KEY",
     "base": "https://generativelanguage.googleapis.com/v1beta",
     "fa": {"generate": "gemini-2.5-flash-image", "edit": "gemini-2.5-flash-image",
            "inpaint": "gemini-2.5-flash-image", "describe": "gemini-2.5-flash"},
     "suggeriti": ["gemini-2.5-flash-image", "gemini-3-pro-image-preview", "gemini-2.5-flash", "gemini-2.5-pro"],
     "nota": "从 aistudio.google.com 获取密钥。"},
    {"id": "stability", "nome": "Stability AI", "tipo": "stability", "chiave": True, "env": "STABILITY_API_KEY",
     "base": "https://api.stability.ai",
     "fa": {"generate": "core", "edit": "sd3.5-large", "inpaint": "inpaint", "upscale": "fast",
            "cutout": "remove-background"},
     "suggeriti": ["core", "ultra", "sd3.5-large", "sd3.5-large-turbo", "sd3.5-medium", "fast", "conservative"],
     "nota": "从 platform.stability.ai 获取密钥，也支持放大和移除背景。"},
    {"id": "openrouter", "nome": "OpenRouter", "tipo": "openrouter", "chiave": True, "env": "OPENROUTER_API_KEY",
     "base": "https://openrouter.ai/api/v1",
     "fa": {"generate": "google/gemini-2.5-flash-image", "edit": "google/gemini-2.5-flash-image",
            "inpaint": "google/gemini-2.5-flash-image", "describe": "google/gemini-2.5-flash"},
     "suggeriti": ["google/gemini-2.5-flash-image", "openai/gpt-5-image", "google/gemini-2.5-flash"],
     "nota": "一个密钥可调用多种图片和视觉模型：openrouter.ai"},
    {"id": "replicate", "nome": "Replicate", "tipo": "replicate", "chiave": True, "env": "REPLICATE_API_TOKEN",
     "base": "https://api.replicate.com/v1",
     "fa": {"generate": "black-forest-labs/flux-schnell", "edit": "black-forest-labs/flux-kontext-pro",
            "inpaint": "black-forest-labs/flux-fill-pro"},
     "suggeriti": ["black-forest-labs/flux-schnell", "black-forest-labs/flux-1.1-pro",
                   "black-forest-labs/flux-kontext-pro", "black-forest-labs/flux-fill-pro"],
     "nota": "支持 replicate.com 上 owner/model 格式的模型，输入可包含 prompt、image、mask。"},
    {"id": "anthropic", "nome": "Anthropic（Claude 视觉模型）", "tipo": "anthropic", "chiave": True,
     "env": "ANTHROPIC_API_KEY", "base": "https://api.anthropic.com/v1",
     "fa": {"describe": "claude-sonnet-5-5"},
     "suggeriti": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
     "nota": "用于描述图片，可与助手共用密钥。"},
    {"id": "ollama", "nome": "Ollama（本机）", "tipo": "openai", "chiave": False,
     "base": "http://localhost:11434/v1", "fa": {"describe": "qwen2.5vl"},
     "suggeriti": ["qwen2.5vl", "llava", "gemma3"],
     "nota": "本地视觉模型，安装命令：ollama pull qwen2.5vl"},
    {"id": "custom", "nome": "其他（兼容 OpenAI）", "tipo": "openai", "chiave": True, "base": "",
     "fa": {"generate": "", "edit": "", "inpaint": "", "describe": ""}, "suggeriti": [],
     "nota": "支持提供 /images/generations、/images/edits 或 /chat/completions 接口的服务。"},
]


class CloudError(RuntimeError):
    pass


def provider(pid: str) -> dict:
    p = next((p for p in PROVIDERS if p["id"] == pid), None)
    if p is None:
        raise CloudError(f"未知 AI 服务 {pid!r}；可选：{', '.join(x['id'] for x in PROVIDERS)}")
    return p


def ids() -> list[str]:
    return [p["id"] for p in PROVIDERS]


# --------------------------------------------------------------------------
# 配置（仅保存在本机）。
# --------------------------------------------------------------------------


def _file() -> Path:
    base = Path(os.environ.get("PEDIT_HOME") or (Path.home() / ".pedit"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "ai_api.json"


def carica() -> dict:
    try:
        cfg = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("default", "")
    for k in ("chiavi", "modelli", "indirizzi"):
        cfg.setdefault(k, {})
    return cfg


def salva(cfg: dict) -> None:
    f = _file()
    tmp = f.with_name(f"~{f.name}")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, f)
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass


def imposta(provider_id: str, chiave: str | None = None, modelli: dict | None = None,
            indirizzo: str | None = None, default: bool | None = None) -> dict:
    """更新配置。``chiave=""`` 删除密钥，``None`` 保持原值。

    ``default=True`` 将该服务设为 ``backend="auto"`` 生成和填充时的默认服务；
    ``False`` 取消默认设置，恢复使用本地 AI。
    """
    p = provider(provider_id)
    cfg = carica()
    if chiave is not None:
        if chiave.strip():
            cfg["chiavi"][provider_id] = chiave.strip()
        else:
            cfg["chiavi"].pop(provider_id, None)
    if modelli:
        attuali = cfg["modelli"].setdefault(provider_id, {})
        for compito, nome in modelli.items():
            if compito not in p["fa"]:
                raise CloudError(f"{p['nome']} 不支持 {compito}；支持的任务：{', '.join(p['fa'])}")
            if nome is None:
                continue
            if str(nome).strip():
                attuali[compito] = str(nome).strip()
            else:
                attuali.pop(compito, None)
    if indirizzo is not None:
        cfg["indirizzi"][provider_id] = indirizzo.strip().rstrip("/")
    if default is True:
        cfg["default"] = provider_id
    elif default is False and cfg["default"] == provider_id:
        cfg["default"] = ""
    salva(cfg)
    return stato()


def _chiave(p: dict, cfg: dict) -> str:
    k = cfg["chiavi"].get(p["id"])
    if k:
        return k
    try:
        from . import llm

        k = llm.carica()["chiavi"].get(p["id"], "")
    except Exception:  # noqa: BLE001 - 聊天配置为可选项。
        k = ""
    return k or (os.environ.get(p["env"], "") if p.get("env") else "")


def modello(p: dict, compito: str, cfg: dict | None = None) -> str:
    cfg = cfg or carica()
    return (cfg["modelli"].get(p["id"]) or {}).get(compito) or p["fa"].get(compito, "")


def _indirizzo(p: dict, cfg: dict) -> str:
    return (cfg["indirizzi"].get(p["id"]) or p.get("base", "")).rstrip("/")


def _pronto(p: dict, cfg: dict, compito: str | None = None) -> str | None:
    """服务可用于指定 ``compito`` 时返回 None，否则返回原因。"""
    if compito is not None and compito not in p["fa"]:
        return f"{p['nome']} 不支持 {compito}"
    if p["chiave"] and not _chiave(p, cfg):
        return f"请粘贴 {p['nome']} API 密钥"
    if not _indirizzo(p, cfg):
        return "缺少服务地址"
    if compito is not None and not modello(p, compito, cfg):
        return f"请为以下任务输入模型：{compito}"
    return None


def _maschera(k: str) -> str:
    return "" if not k else (k[:4] + "…" + k[-4:] if len(k) > 12 else "••••")


def stato() -> dict:
    """返回界面和智能体可见的状态，绝不返回完整密钥。"""
    cfg = carica()
    elenco = []
    for p in PROVIDERS:
        k = _chiave(p, cfg)
        elenco.append({
            "id": p["id"], "nome": p["nome"], "vuole_chiave": p["chiave"], "nota": p.get("nota", ""),
            "fa": list(p["fa"]), "modelli": {c: modello(p, c, cfg) for c in p["fa"]},
            "suggeriti": p.get("suggeriti", []), "indirizzo": _indirizzo(p, cfg),
            "indirizzo_modificabile": p["tipo"] == "openai",
            "chiave": _maschera(k), "chiave_da_altrove": bool(k) and p["id"] not in cfg["chiavi"],
            "pronto": _pronto(p, cfg) is None, "motivo": _pronto(p, cfg),
        })
    return {"default": cfg["default"], "providers": elenco}


def riassunto() -> dict:
    """供 ai_status 使用：报告可用服务及其能力。"""
    cfg = carica()
    pronti = {p["id"]: list(p["fa"]) for p in PROVIDERS if p["chiave"] and _pronto(p, cfg) is None}
    return {"default": cfg["default"] or None, "ready": pronti,
            "hint": "" if pronti else "请在“AI 模型”设置中粘贴 API 密钥（OpenAI、Gemini、Stability、"
                                      "OpenRouter、Replicate、Anthropic），以使用在线模型"}


def scegli(compito: str, backend: str = "auto") -> str | None:
    """选择执行 ``compito`` 的服务。

    ``backend`` 为服务 ID 时直接使用该服务，不可用时给出明确错误；
    ``auto`` 优先使用支持该任务的默认服务，否则返回 None，交给本地 AI。
    ``describe`` 无内置本地实现，自动选择首个已配置密钥的服务。
    """
    cfg = carica()
    if backend not in ("auto", None):
        p = provider(backend)
        motivo = _pronto(p, cfg, compito)
        if motivo:
            raise CloudError(motivo)
        return p["id"]
    d = cfg["default"]
    if d and d in ids() and _pronto(provider(d), cfg, compito) is None:
        return d
    if compito == "describe":
        for p in PROVIDERS:
            if p["chiave"] and _pronto(p, cfg, compito) is None:
                return p["id"]
    return None


# --------------------------------------------------------------------------
# HTTP 请求
# --------------------------------------------------------------------------


def _http(method: str, url: str, headers: dict, body: bytes | None = None,
          timeout: float = 300.0) -> tuple[int, dict, bytes]:
    """唯一的网络出口；即使 HTTP 出错，也返回状态码、响应头和响应体。"""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        host = urllib.parse.urlsplit(url).netloc
        raise CloudError(f"无法连接 {host}: {exc}") from exc


def _errore(nome: str, stato: int, corpo: bytes) -> CloudError:
    testo = corpo.decode("utf-8", "replace")
    try:
        j = json.loads(testo)
        e = j.get("error") if isinstance(j, dict) else None
        if isinstance(e, dict):
            testo = e.get("message") or json.dumps(e)
        elif isinstance(e, str):
            testo = e
        elif isinstance(j, dict) and j.get("errors"):
            testo = "; ".join(map(str, j["errors"]))
        elif isinstance(j, dict) and j.get("detail"):
            testo = str(j["detail"])
    except ValueError:
        pass
    if stato in (401, 403):
        return CloudError(f"{nome}：密钥无效或权限不足（{stato}): {testo[:300]}")
    if stato == 402:
        return CloudError(f"{nome}：余额不足（{testo[:300]})")
    if stato == 429:
        return CloudError(f"{nome}：达到速率或配额限制，请稍后重试（{testo[:300]})")
    return CloudError(f"{nome} 拒绝了请求（{stato}): {testo[:400]}")


def _json(nome: str, method: str, url: str, headers: dict, body: dict | None = None,
          timeout: float = 300.0) -> Any:
    h = {"Content-Type": "application/json", **headers}
    stato, _, corpo = _http(method, url, h, json.dumps(body).encode() if body is not None else None, timeout)
    if stato >= 400:
        raise _errore(nome, stato, corpo)
    try:
        return json.loads(corpo or b"{}")
    except ValueError as exc:
        raise CloudError(f"{nome}：响应不是 JSON") from exc


def _multipart(campi: dict[str, Any]) -> tuple[bytes, str]:
    """编码文本字段和文件（``(文件名, bytes, MIME 类型)``），列表值生成重复字段。"""
    b = uuid.uuid4().hex
    parti: list[bytes] = []
    for k, v in campi.items():
        for x in (v if isinstance(v, list) else [v]):
            if x is None:
                continue
            if isinstance(x, tuple):
                fname, data, mime = x
                parti.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\n"
                             f"Content-Type: {mime}\r\n\r\n".encode() + data + b"\r\n")
            else:
                parti.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{x}\r\n".encode())
    parti.append(f"--{b}--\r\n".encode())
    return b"".join(parti), f"multipart/form-data; boundary={b}"


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _data_uri(img: Image.Image) -> str:
    return "data:image/png;base64," + base64.b64encode(_png(img.convert("RGB"))).decode()


def _immagine(data: bytes) -> Image.Image:
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        return im.convert("RGBA")
    except OSError as exc:
        raise CloudError("服务未返回图片") from exc


def _scarica(url: str) -> Image.Image:
    if url.startswith("data:"):
        return _immagine(base64.b64decode(url.split(",", 1)[1]))
    stato, _, corpo = _http("GET", url, {"User-Agent": "pedit"})
    if stato >= 400:
        raise CloudError(f"下载结果失败（{stato})")
    return _immagine(corpo)


def _riduci(img: Image.Image, lato: int = 1024) -> Image.Image:
    k = lato / max(img.size)
    if k >= 1:
        return img
    return img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))), Image.Resampling.LANCZOS)


_RAPPORTI = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9", "9:21")


def rapporto(w: int, h: int, scelte: tuple[str, ...] = _RAPPORTI) -> str:
    """选择最接近 w:h 的可用宽高比。"""
    import math

    r = math.log(w / max(h, 1))
    return min(scelte, key=lambda s: abs(math.log(int(s.split(":")[0]) / int(s.split(":")[1])) - r))


def _misura_openai(w: int, h: int, model: str) -> str:
    if model.startswith("dall-e-2"):
        return "1024x1024"
    if model.startswith("dall-e-3"):
        return {"1:1": "1024x1024", "16:9": "1792x1024", "9:16": "1024x1792"}[rapporto(w, h, ("1:1", "16:9", "9:16"))]
    return {"1:1": "1024x1024", "3:2": "1536x1024", "2:3": "1024x1536"}[rapporto(w, h, ("1:1", "3:2", "2:3"))]


# --------------------------------------------------------------------------
# 服务
# --------------------------------------------------------------------------


def _ctx(pid: str, compito: str) -> tuple[dict, str, str, str]:
    cfg = carica()
    p = provider(pid)
    motivo = _pronto(p, cfg, compito)
    if motivo:
        raise CloudError(motivo)
    return p, _chiave(p, cfg), _indirizzo(p, cfg), modello(p, compito, cfg)


def _auth(p: dict, chiave: str) -> dict:
    return {"Authorization": f"Bearer {chiave}"} if chiave else {}


# ---- OpenAI 及兼容服务 ---------------------------------------------------

def _openai_risultato(p: dict, j: dict) -> Image.Image:
    data = (j.get("data") or [{}])[0]
    if data.get("b64_json"):
        return _immagine(base64.b64decode(data["b64_json"]))
    if data.get("url"):
        return _scarica(data["url"])
    raise CloudError(f"{p['nome']} 未返回图片")


def _openai_genera(p, chiave, base, model, prompt, w, h, negative, seed):
    body = {"model": model, "prompt": prompt + (f"\nAvoid: {negative}" if negative else ""), "n": 1,
            "size": _misura_openai(w, h, model)}
    if model.startswith("dall-e"):
        body["response_format"] = "b64_json"
    return _openai_risultato(p, _json(p["nome"], "POST", f"{base}/images/generations", _auth(p, chiave), body))


def _openai_modifica(p, chiave, base, model, img, prompt, mask=None):
    w, h = img.size
    campi: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1, "size": _misura_openai(w, h, model),
                             "image": ("image.png", _png(img.convert("RGBA")), "image/png")}
    if mask is not None:
        # OpenAI 使用蒙版中的透明区域表示需要重绘的部分。
        m = Image.new("RGBA", img.size, (0, 0, 0, 255))
        m.putalpha(Image.eval(mask.convert("L").resize(img.size), lambda v: 255 - v))
        campi["mask"] = ("mask.png", _png(m), "image/png")
    if model.startswith("dall-e"):
        campi["response_format"] = "b64_json"
    corpo, ctype = _multipart(campi)
    stato, _, risposta = _http("POST", f"{base}/images/edits", {**_auth(p, chiave), "Content-Type": ctype}, corpo)
    if stato >= 400:
        raise _errore(p["nome"], stato, risposta)
    return _openai_risultato(p, json.loads(risposta))


def _openai_descrivi(p, chiave, base, model, img, domanda):
    body = {"model": model, "messages": [{"role": "user", "content": [
        {"type": "text", "text": domanda},
        {"type": "image_url", "image_url": {"url": _data_uri(img)}}]}]}
    j = _json(p["nome"], "POST", f"{base}/chat/completions", _auth(p, chiave), body)
    try:
        testo = j["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise CloudError(f"{p['nome']} 未返回文字") from exc
    if isinstance(testo, list):
        testo = "".join(x.get("text", "") for x in testo if isinstance(x, dict))
    return (testo or "").strip()


# ---- Gemini 服务 ---------------------------------------------------------

def _gemini(p, chiave, base, model, parti, immagine=True, aspetto=None):
    gen: dict[str, Any] = {}
    if immagine:
        gen["responseModalities"] = ["TEXT", "IMAGE"]
        if aspetto:
            gen["imageConfig"] = {"aspectRatio": aspetto}
    body = {"contents": [{"role": "user", "parts": parti}], **({"generationConfig": gen} if gen else {})}
    url = f"{base}/models/{urllib.parse.quote(model, safe='-._')}:generateContent"
    j = _json(p["nome"], "POST", url, {"x-goog-api-key": chiave}, body)
    testi, immagini = [], []
    for c in j.get("candidates") or []:
        for parte in (c.get("content") or {}).get("parts") or []:
            dati = parte.get("inlineData") or parte.get("inline_data")
            if dati and dati.get("data"):
                immagini.append(_immagine(base64.b64decode(dati["data"])))
            elif parte.get("text"):
                testi.append(parte["text"])
    if immagine:
        if not immagini:
            motivo = " ".join(testi)[:300] or (j.get("promptFeedback") or {}).get("blockReason", "")
            raise CloudError(f"{p['nome']} 未返回图片{': ' + motivo if motivo else ''}")
        return immagini[0]
    return "\n".join(testi).strip()


def _parte_img(img: Image.Image) -> dict:
    return {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(_png(img.convert("RGB"))).decode()}}


# ---- Stability 服务 ------------------------------------------------------

def _stability(p, chiave, base, percorso, campi):
    corpo, ctype = _multipart({**campi, "output_format": "png"})
    stato, _, risposta = _http("POST", f"{base}/v2beta/{percorso}",
                               {"Authorization": f"Bearer {chiave}", "Accept": "image/*", "Content-Type": ctype}, corpo)
    if stato >= 400:
        raise _errore(p["nome"], stato, risposta)
    return _immagine(risposta)


def _file_png(img: Image.Image, nome: str = "image.png") -> tuple:
    return (nome, _png(img), "image/png")


# ---- OpenRouter 服务 -----------------------------------------------------

def _openrouter_img(p, chiave, base, model, testo, immagini, aspetto=None):
    contenuto: list[dict] = [{"type": "text", "text": testo}]
    contenuto += [{"type": "image_url", "image_url": {"url": _data_uri(im)}} for im in immagini]
    body: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": contenuto}],
                            "modalities": ["image", "text"]}
    if aspetto:
        body["image_config"] = {"aspect_ratio": aspetto}
    j = _json(p["nome"], "POST", f"{base}/chat/completions", _auth(p, chiave), body)
    try:
        msg = j["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise CloudError(f"{p['nome']} 未返回回答") from exc
    for im in msg.get("images") or []:
        url = (im.get("image_url") or {}).get("url") if isinstance(im, dict) else None
        if url:
            return _scarica(url)
    raise CloudError(f"{p['nome']} 未返回图片（{model} 是否为图片模型？）")


# ---- Replicate 服务 ------------------------------------------------------

def _replicate(p, chiave, base, model, entrate, attesa: float = 600.0):
    if "/" not in model:
        raise CloudError("Replicate 模型应为 owner/name 格式，例如 black-forest-labs/flux-schnell")
    owner_nome, _, versione = model.partition(":")
    h = {**_auth(p, chiave), "Prefer": "wait=60"}
    if versione:
        j = _json(p["nome"], "POST", f"{base}/predictions", h, {"version": versione, "input": entrate})
    else:
        j = _json(p["nome"], "POST", f"{base}/models/{owner_nome}/predictions", h, {"input": entrate})
    t0 = time.monotonic()
    while j.get("status") not in ("succeeded", "failed", "canceled"):
        if time.monotonic() - t0 > attesa:
            raise CloudError(f"{p['nome']}：预测执行超时")
        time.sleep(1.5)
        url = (j.get("urls") or {}).get("get") or f"{base}/predictions/{j.get('id')}"
        j = _json(p["nome"], "GET", url, _auth(p, chiave))
    if j.get("status") != "succeeded":
        raise CloudError(f"{p['nome']}: {j.get('error') or j.get('status')}")
    out = j.get("output")
    if isinstance(out, list):
        out = out[0] if out else None
    if not isinstance(out, str):
        raise CloudError(f"{p['nome']} 未返回图片")
    return _scarica(out)


def _replicate_img_key(model: str) -> str:
    return "input_image" if "kontext" in model else "image"


# ---- Anthropic 服务 ------------------------------------------------------

def _anthropic_descrivi(p, chiave, base, model, img, domanda):
    body = {"model": model, "max_tokens": 1024, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                      "data": base64.b64encode(_png(img.convert("RGB"))).decode()}},
        {"type": "text", "text": domanda}]}]}
    j = _json(p["nome"], "POST", f"{base}/messages", {"x-api-key": chiave, "anthropic-version": "2023-06-01"}, body)
    return "".join(b.get("text", "") for b in j.get("content") or [] if b.get("type") == "text").strip()


# --------------------------------------------------------------------------
# 任务
# --------------------------------------------------------------------------


def generate(pid: str, prompt: str, width: int, height: int, negative: str | None = None,
             seed: int | None = None) -> Image.Image:
    p, chiave, base, model = _ctx(pid, "generate")
    t = p["tipo"]
    if t == "openai":
        return _openai_genera(p, chiave, base, model, prompt, width, height, negative, seed)
    if t == "gemini":
        testo = prompt + (f"\nAvoid: {negative}" if negative else "")
        return _gemini(p, chiave, base, model, [{"text": testo}], aspetto=rapporto(width, height))
    if t == "stability":
        campi = {"prompt": prompt, "negative_prompt": negative, "aspect_ratio": rapporto(
            width, height, ("1:1", "2:3", "3:2", "4:5", "5:4", "9:16", "16:9", "21:9", "9:21")),
            "seed": seed if seed is not None and seed >= 0 else None}
        if model.startswith("sd3"):
            return _stability(p, chiave, base, "stable-image/generate/sd3", {**campi, "model": model})
        return _stability(p, chiave, base, f"stable-image/generate/{model}", campi)
    if t == "openrouter":
        return _openrouter_img(p, chiave, base, model, prompt + (f"\nAvoid: {negative}" if negative else ""), [],
                               aspetto=rapporto(width, height))
    if t == "replicate":
        entrate: dict[str, Any] = {"prompt": prompt, "aspect_ratio": rapporto(width, height), "output_format": "png"}
        if seed is not None and seed >= 0:
            entrate["seed"] = int(seed)
        return _replicate(p, chiave, base, model, entrate)
    raise CloudError(f"{p['nome']} 不支持生成图片")


def edit(pid: str, img: Image.Image, prompt: str, strength: float = 0.6, seed: int | None = None) -> Image.Image:
    """图生图：按提示词修改图片，返回与 ``img`` 相同的尺寸。"""
    p, chiave, base, model = _ctx(pid, "edit")
    t = p["tipo"]
    src = _riduci(img.convert("RGB"))
    if t == "openai":
        out = _openai_modifica(p, chiave, base, model, src, prompt)
    elif t == "gemini":
        out = _gemini(p, chiave, base, model, [{"text": prompt}, _parte_img(src)], aspetto=rapporto(*src.size))
    elif t == "stability":
        campi = {"prompt": prompt, "image": _file_png(src), "strength": f"{min(1.0, max(0.0, strength)):.2f}",
                 "seed": seed if seed is not None and seed >= 0 else None}
        if model == "ultra":
            out = _stability(p, chiave, base, "stable-image/generate/ultra", campi)
        else:
            out = _stability(p, chiave, base, "stable-image/generate/sd3",
                             {**campi, "mode": "image-to-image", "model": model if model.startswith("sd3") else "sd3.5-large"})
    elif t == "openrouter":
        out = _openrouter_img(p, chiave, base, model, prompt, [src])
    elif t == "replicate":
        entrate = {"prompt": prompt, _replicate_img_key(model): _data_uri(src), "output_format": "png"}
        if "kontext" not in model:
            entrate["prompt_strength"] = float(strength)
        out = _replicate(p, chiave, base, model, entrate)
    else:
        raise CloudError(f"{p['nome']} 不支持编辑图片")
    return out.convert("RGB").resize(img.size, Image.Resampling.LANCZOS)


def inpaint(pid: str, img: Image.Image, mask: Image.Image, prompt: str, seed: int | None = None) -> Image.Image:
    """重绘 ``mask`` 中的白色区域，返回与 ``img`` 相同尺寸的图片。

    不支持蒙版的服务（Gemini、OpenRouter）将蒙版作为第二张图片接收，
    并附带编辑指令。``Store`` 始终只保留蒙版区域，丢弃区域外的修改。
    """
    p, chiave, base, model = _ctx(pid, "inpaint")
    t = p["tipo"]
    src = _riduci(img.convert("RGB"))
    m = mask.convert("L").resize(src.size, Image.Resampling.BILINEAR)
    istruzione = (f"Edit the first image: change ONLY the area that is white in the second image (a mask) so it "
                  f"shows: {prompt}. Keep everything else identical, same framing and size, seamless.")
    if t == "openai":
        out = _openai_modifica(p, chiave, base, model, src, prompt, mask=m)
    elif t == "stability":
        out = _stability(p, chiave, base, "stable-image/edit/inpaint", {
            "prompt": prompt, "image": _file_png(src), "mask": _file_png(m, "mask.png"),
            "seed": seed if seed is not None and seed >= 0 else None})
    elif t == "gemini":
        out = _gemini(p, chiave, base, model, [{"text": istruzione}, _parte_img(src), _parte_img(m.convert("RGB"))],
                      aspetto=rapporto(*src.size))
    elif t == "openrouter":
        out = _openrouter_img(p, chiave, base, model, istruzione, [src, m.convert("RGB")])
    elif t == "replicate":
        out = _replicate(p, chiave, base, model, {"prompt": prompt, "image": _data_uri(src),
                                                  "mask": _data_uri(m.convert("RGB")), "output_format": "png"})
    else:
        raise CloudError(f"{p['nome']} 不支持填充图片")
    return out.convert("RGB").resize(img.size, Image.Resampling.LANCZOS)


def describe(pid: str, img: Image.Image, question: str | None = None) -> str:
    p, chiave, base, model = _ctx(pid, "describe")
    domanda = question or ("请用简体中文准确描述这张图片：主体、环境、构图、光线、颜色、"
                           "风格及可见文字。保持简洁。")
    src = _riduci(img.convert("RGB"), 1536)
    t = p["tipo"]
    if t == "openai":
        return _openai_descrivi(p, chiave, base, model, src, domanda)
    if t == "openrouter":
        return _openai_descrivi(p, chiave, base, model, src, domanda)
    if t == "gemini":
        return _gemini(p, chiave, base, model, [{"text": domanda}, _parte_img(src)], immagine=False)
    if t == "anthropic":
        return _anthropic_descrivi(p, chiave, base, model, src, domanda)
    raise CloudError(f"{p['nome']} 不支持描述图片")


def upscale(pid: str, img: Image.Image) -> Image.Image:
    p, chiave, base, model = _ctx(pid, "upscale")
    if p["tipo"] != "stability":
        raise CloudError(f"{p['nome']} 不支持放大图片")
    src = img.convert("RGB")
    if model == "fast":
        return _stability(p, chiave, base, "stable-image/upscale/fast", {"image": _file_png(src)})
    return _stability(p, chiave, base, f"stable-image/upscale/{model}",
                      {"image": _file_png(src), "prompt": "high quality, detailed photo"})


def cutout(pid: str, img: Image.Image) -> Image.Image:
    """从服务的抠图结果提取主体 L 模式蒙版（255 表示主体）。"""
    p, chiave, base, _ = _ctx(pid, "cutout")
    if p["tipo"] != "stability":
        raise CloudError(f"{p['nome']} 不支持移除背景")
    out = _stability(p, chiave, base, "stable-image/edit/remove-background", {"image": _file_png(img.convert("RGB"))})
    return out.getchannel("A").resize(img.size, Image.Resampling.BILINEAR)
