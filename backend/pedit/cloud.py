"""Modelli di immagini via API, con la chiave di chi usa l'editor.

Accanto all'IA locale (rembg, OpenCV, ComfyUI) pedit sa usare servizi in rete:
generazione da testo, modifica da immagine (image-to-image), riempimento con
maschera, descrizione di un'immagine (image-to-text), upscale e scontorno.
Ogni servizio dichiara in ``fa`` i compiti che sa fare e per ognuno un modello
predefinito, che si cambia nelle impostazioni.

- ``openai``: GPT Image (``/images/generations`` e ``/images/edits``), visione
  con ``/chat/completions``. Lo stesso protocollo vale per ``custom``: qualunque
  servizio compatibile OpenAI (anche locale) con indirizzo e chiave;
- ``gemini``: Google Gemini immagini ("Nano Banana") con ``generateContent``;
- ``stability``: Stability AI v2beta (generate, inpaint, upscale, remove-background);
- ``openrouter``: modelli di immagini via chat con ``modalities: ["image"]``;
- ``replicate``: qualunque modello ``owner/nome`` (FLUX e simili);
- ``anthropic``: Claude per descrivere le immagini;
- ``ollama``: un modello di visione sul computer, senza chiave.

Le chiavi stanno in ``~/.pedit/ai_api.json``, mai nel documento. Se manca, vale
quella dell'assistente (``llm.json``, stessi id) o la variabile d'ambiente: una
chiave OpenAI incollata una volta serve alla chat e alle immagini.

Qui si parla con i servizi e basta: immagini PIL in entrata e in uscita. Dove
mettere il risultato nel documento lo decide ``Store``. Le chiamate HTTP
passano tutte da ``_http``, cosi' i test la sostituiscono e non vanno in rete.
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
     "nota": "Key from platform.openai.com. The same key as the assistant works."},
    {"id": "gemini", "nome": "Google Gemini (Nano Banana)", "tipo": "gemini", "chiave": True, "env": "GEMINI_API_KEY",
     "base": "https://generativelanguage.googleapis.com/v1beta",
     "fa": {"generate": "gemini-2.5-flash-image", "edit": "gemini-2.5-flash-image",
            "inpaint": "gemini-2.5-flash-image", "describe": "gemini-2.5-flash"},
     "suggeriti": ["gemini-2.5-flash-image", "gemini-3-pro-image-preview", "gemini-2.5-flash", "gemini-2.5-pro"],
     "nota": "Key from aistudio.google.com"},
    {"id": "stability", "nome": "Stability AI", "tipo": "stability", "chiave": True, "env": "STABILITY_API_KEY",
     "base": "https://api.stability.ai",
     "fa": {"generate": "core", "edit": "sd3.5-large", "inpaint": "inpaint", "upscale": "fast",
            "cutout": "remove-background"},
     "suggeriti": ["core", "ultra", "sd3.5-large", "sd3.5-large-turbo", "sd3.5-medium", "fast", "conservative"],
     "nota": "Key from platform.stability.ai. Also does upscale and background removal."},
    {"id": "openrouter", "nome": "OpenRouter", "tipo": "openrouter", "chiave": True, "env": "OPENROUTER_API_KEY",
     "base": "https://openrouter.ai/api/v1",
     "fa": {"generate": "google/gemini-2.5-flash-image", "edit": "google/gemini-2.5-flash-image",
            "inpaint": "google/gemini-2.5-flash-image", "describe": "google/gemini-2.5-flash"},
     "suggeriti": ["google/gemini-2.5-flash-image", "openai/gpt-5-image", "google/gemini-2.5-flash"],
     "nota": "One key for many image and vision models: openrouter.ai"},
    {"id": "replicate", "nome": "Replicate", "tipo": "replicate", "chiave": True, "env": "REPLICATE_API_TOKEN",
     "base": "https://api.replicate.com/v1",
     "fa": {"generate": "black-forest-labs/flux-schnell", "edit": "black-forest-labs/flux-kontext-pro",
            "inpaint": "black-forest-labs/flux-fill-pro"},
     "suggeriti": ["black-forest-labs/flux-schnell", "black-forest-labs/flux-1.1-pro",
                   "black-forest-labs/flux-kontext-pro", "black-forest-labs/flux-fill-pro"],
     "nota": "Any owner/model from replicate.com (prompt, image, mask inputs)"},
    {"id": "anthropic", "nome": "Anthropic (Claude vision)", "tipo": "anthropic", "chiave": True,
     "env": "ANTHROPIC_API_KEY", "base": "https://api.anthropic.com/v1",
     "fa": {"describe": "claude-sonnet-5-5"},
     "suggeriti": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
     "nota": "Describes images. The same key as the assistant works."},
    {"id": "ollama", "nome": "Ollama (on this computer)", "tipo": "openai", "chiave": False,
     "base": "http://localhost:11434/v1", "fa": {"describe": "qwen2.5vl"},
     "suggeriti": ["qwen2.5vl", "llava", "gemma3"],
     "nota": "A local vision model: ollama pull qwen2.5vl"},
    {"id": "custom", "nome": "Other (OpenAI compatible)", "tipo": "openai", "chiave": True, "base": "",
     "fa": {"generate": "", "edit": "", "inpaint": "", "describe": ""}, "suggeriti": [],
     "nota": "Any service exposing /images/generations, /images/edits or /chat/completions"},
]


class CloudError(RuntimeError):
    pass


def provider(pid: str) -> dict:
    p = next((p for p in PROVIDERS if p["id"] == pid), None)
    if p is None:
        raise CloudError(f"unknown AI service {pid!r}; available: {', '.join(x['id'] for x in PROVIDERS)}")
    return p


def ids() -> list[str]:
    return [p["id"] for p in PROVIDERS]


# --------------------------------------------------------------------------
# configurazione (solo su questa macchina)
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
    """Aggiorna la configurazione. ``chiave=""`` la cancella, ``None`` la lascia com'e'.

    ``default=True`` fa di questo servizio quello che ``backend="auto"`` usa per
    generare e riempire; ``False`` lo toglie (si torna all'IA locale).
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
                raise CloudError(f"{p['nome']} does not do {compito}; it does {', '.join(p['fa'])}")
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
    except Exception:  # noqa: BLE001 - la configurazione della chat non e' obbligatoria
        k = ""
    return k or (os.environ.get(p["env"], "") if p.get("env") else "")


def modello(p: dict, compito: str, cfg: dict | None = None) -> str:
    cfg = cfg or carica()
    return (cfg["modelli"].get(p["id"]) or {}).get(compito) or p["fa"].get(compito, "")


def _indirizzo(p: dict, cfg: dict) -> str:
    return (cfg["indirizzi"].get(p["id"]) or p.get("base", "")).rstrip("/")


def _pronto(p: dict, cfg: dict, compito: str | None = None) -> str | None:
    """None se il servizio si puo' usare (per ``compito``), altrimenti il motivo in parole."""
    if compito is not None and compito not in p["fa"]:
        return f"{p['nome']} does not do {compito}"
    if p["chiave"] and not _chiave(p, cfg):
        return f"paste the {p['nome']} API key"
    if not _indirizzo(p, cfg):
        return "the service address is missing"
    if compito is not None and not modello(p, compito, cfg):
        return f"type the model for {compito}"
    return None


def _maschera(k: str) -> str:
    return "" if not k else (k[:4] + "…" + k[-4:] if len(k) > 12 else "••••")


def stato() -> dict:
    """Quello che vede la UI (e l'agente): mai le chiavi intere."""
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
    """Per ai_status: quali servizi sono pronti e per cosa."""
    cfg = carica()
    pronti = {p["id"]: list(p["fa"]) for p in PROVIDERS if p["chiave"] and _pronto(p, cfg) is None}
    return {"default": cfg["default"] or None, "ready": pronti,
            "hint": "" if pronti else "paste an API key in AI models settings (OpenAI, Gemini, Stability, "
                                      "OpenRouter, Replicate, Anthropic) to use online models"}


def scegli(compito: str, backend: str = "auto") -> str | None:
    """Il servizio da usare per ``compito``.

    ``backend`` = id di un servizio -> quello (con errore chiaro se non va);
    ``auto`` -> il servizio predefinito se sa farlo, altrimenti None (IA locale).
    Per ``describe``, che in locale non c'e', ``auto`` prende il primo con la chiave.
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
# HTTP
# --------------------------------------------------------------------------


def _http(method: str, url: str, headers: dict, body: bytes | None = None,
          timeout: float = 300.0) -> tuple[int, dict, bytes]:
    """L'unica uscita verso la rete. Ritorna (stato, header, corpo) anche per gli errori HTTP."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        host = urllib.parse.urlsplit(url).netloc
        raise CloudError(f"cannot reach {host}: {exc}") from exc


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
        return CloudError(f"{nome}: invalid key or missing permissions ({stato}): {testo[:300]}")
    if stato == 402:
        return CloudError(f"{nome}: out of credits ({testo[:300]})")
    if stato == 429:
        return CloudError(f"{nome}: rate limit or quota reached, retry later ({testo[:300]})")
    return CloudError(f"{nome} refused the request ({stato}): {testo[:400]}")


def _json(nome: str, method: str, url: str, headers: dict, body: dict | None = None,
          timeout: float = 300.0) -> Any:
    h = {"Content-Type": "application/json", **headers}
    stato, _, corpo = _http(method, url, h, json.dumps(body).encode() if body is not None else None, timeout)
    if stato >= 400:
        raise _errore(nome, stato, corpo)
    try:
        return json.loads(corpo or b"{}")
    except ValueError as exc:
        raise CloudError(f"{nome}: the answer is not JSON") from exc


def _multipart(campi: dict[str, Any]) -> tuple[bytes, str]:
    """Campi testo e file (``(nome_file, bytes, mime)``). Le liste ripetono il campo."""
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
        raise CloudError("the service did not return an image") from exc


def _scarica(url: str) -> Image.Image:
    if url.startswith("data:"):
        return _immagine(base64.b64decode(url.split(",", 1)[1]))
    stato, _, corpo = _http("GET", url, {"User-Agent": "pedit"})
    if stato >= 400:
        raise CloudError(f"downloading the result failed ({stato})")
    return _immagine(corpo)


def _riduci(img: Image.Image, lato: int = 1024) -> Image.Image:
    k = lato / max(img.size)
    if k >= 1:
        return img
    return img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))), Image.Resampling.LANCZOS)


_RAPPORTI = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9", "9:21")


def rapporto(w: int, h: int, scelte: tuple[str, ...] = _RAPPORTI) -> str:
    """Il rapporto d'aspetto ammesso piu' vicino a w:h."""
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
# servizi
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


# ---- OpenAI e compatibili ---------------------------------------------------

def _openai_risultato(p: dict, j: dict) -> Image.Image:
    data = (j.get("data") or [{}])[0]
    if data.get("b64_json"):
        return _immagine(base64.b64decode(data["b64_json"]))
    if data.get("url"):
        return _scarica(data["url"])
    raise CloudError(f"{p['nome']} returned no image")


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
        # OpenAI: la zona da rifare e' quella TRASPARENTE della maschera
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
        raise CloudError(f"{p['nome']} returned no text") from exc
    if isinstance(testo, list):
        testo = "".join(x.get("text", "") for x in testo if isinstance(x, dict))
    return (testo or "").strip()


# ---- Gemini -----------------------------------------------------------------

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
            raise CloudError(f"{p['nome']} returned no image{': ' + motivo if motivo else ''}")
        return immagini[0]
    return "\n".join(testi).strip()


def _parte_img(img: Image.Image) -> dict:
    return {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(_png(img.convert("RGB"))).decode()}}


# ---- Stability --------------------------------------------------------------

def _stability(p, chiave, base, percorso, campi):
    corpo, ctype = _multipart({**campi, "output_format": "png"})
    stato, _, risposta = _http("POST", f"{base}/v2beta/{percorso}",
                               {"Authorization": f"Bearer {chiave}", "Accept": "image/*", "Content-Type": ctype}, corpo)
    if stato >= 400:
        raise _errore(p["nome"], stato, risposta)
    return _immagine(risposta)


def _file_png(img: Image.Image, nome: str = "image.png") -> tuple:
    return (nome, _png(img), "image/png")


# ---- OpenRouter ---------------------------------------------------------------

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
        raise CloudError(f"{p['nome']} returned no answer") from exc
    for im in msg.get("images") or []:
        url = (im.get("image_url") or {}).get("url") if isinstance(im, dict) else None
        if url:
            return _scarica(url)
    raise CloudError(f"{p['nome']} returned no image (is {model} an image model?)")


# ---- Replicate ----------------------------------------------------------------

def _replicate(p, chiave, base, model, entrate, attesa: float = 600.0):
    if "/" not in model:
        raise CloudError("a Replicate model is owner/name, e.g. black-forest-labs/flux-schnell")
    owner_nome, _, versione = model.partition(":")
    h = {**_auth(p, chiave), "Prefer": "wait=60"}
    if versione:
        j = _json(p["nome"], "POST", f"{base}/predictions", h, {"version": versione, "input": entrate})
    else:
        j = _json(p["nome"], "POST", f"{base}/models/{owner_nome}/predictions", h, {"input": entrate})
    t0 = time.monotonic()
    while j.get("status") not in ("succeeded", "failed", "canceled"):
        if time.monotonic() - t0 > attesa:
            raise CloudError(f"{p['nome']}: the prediction took too long")
        time.sleep(1.5)
        url = (j.get("urls") or {}).get("get") or f"{base}/predictions/{j.get('id')}"
        j = _json(p["nome"], "GET", url, _auth(p, chiave))
    if j.get("status") != "succeeded":
        raise CloudError(f"{p['nome']}: {j.get('error') or j.get('status')}")
    out = j.get("output")
    if isinstance(out, list):
        out = out[0] if out else None
    if not isinstance(out, str):
        raise CloudError(f"{p['nome']} returned no image")
    return _scarica(out)


def _replicate_img_key(model: str) -> str:
    return "input_image" if "kontext" in model else "image"


# ---- Anthropic ----------------------------------------------------------------

def _anthropic_descrivi(p, chiave, base, model, img, domanda):
    body = {"model": model, "max_tokens": 1024, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                      "data": base64.b64encode(_png(img.convert("RGB"))).decode()}},
        {"type": "text", "text": domanda}]}]}
    j = _json(p["nome"], "POST", f"{base}/messages", {"x-api-key": chiave, "anthropic-version": "2023-06-01"}, body)
    return "".join(b.get("text", "") for b in j.get("content") or [] if b.get("type") == "text").strip()


# --------------------------------------------------------------------------
# compiti
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
    raise CloudError(f"{p['nome']} does not generate images")


def edit(pid: str, img: Image.Image, prompt: str, strength: float = 0.6, seed: int | None = None) -> Image.Image:
    """Image-to-image: la stessa immagine cambiata secondo il prompt. Ritorna la misura di ``img``."""
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
        raise CloudError(f"{p['nome']} does not edit images")
    return out.convert("RGB").resize(img.size, Image.Resampling.LANCZOS)


def inpaint(pid: str, img: Image.Image, mask: Image.Image, prompt: str, seed: int | None = None) -> Image.Image:
    """Rifa la zona bianca di ``mask``. Ritorna un'immagine grande come ``img``.

    I servizi senza maschera (Gemini, OpenRouter) ricevono la maschera come
    seconda immagine con un'istruzione; in ogni caso ``Store`` tiene solo la
    zona mascherata, quindi un ritocco fuori zona non passa.
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
        raise CloudError(f"{p['nome']} does not fill images")
    return out.convert("RGB").resize(img.size, Image.Resampling.LANCZOS)


def describe(pid: str, img: Image.Image, question: str | None = None) -> str:
    p, chiave, base, model = _ctx(pid, "describe")
    domanda = question or ("Describe this image precisely: subject, setting, composition, light, colors, "
                           "style, and any visible text. Be concise.")
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
    raise CloudError(f"{p['nome']} does not describe images")


def upscale(pid: str, img: Image.Image) -> Image.Image:
    p, chiave, base, model = _ctx(pid, "upscale")
    if p["tipo"] != "stability":
        raise CloudError(f"{p['nome']} does not upscale")
    src = img.convert("RGB")
    if model == "fast":
        return _stability(p, chiave, base, "stable-image/upscale/fast", {"image": _file_png(src)})
    return _stability(p, chiave, base, f"stable-image/upscale/{model}",
                      {"image": _file_png(src), "prompt": "high quality, detailed photo"})


def cutout(pid: str, img: Image.Image) -> Image.Image:
    """Maschera L del soggetto (255 = soggetto) dallo scontorno del servizio."""
    p, chiave, base, _ = _ctx(pid, "cutout")
    if p["tipo"] != "stability":
        raise CloudError(f"{p['nome']} does not remove backgrounds")
    out = _stability(p, chiave, base, "stable-image/edit/remove-background", {"image": _file_png(img.convert("RGB"))})
    return out.getchannel("A").resize(img.size, Image.Resampling.BILINEAR)
