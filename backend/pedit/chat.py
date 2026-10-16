"""Assistente in chat dentro l'editor.

I suoi strumenti *sono* quelli del server MCP (``mcp_server.STRUMENTI``), con
gli stessi schemi: un solo elenco da mantenere, e quello che fa l'assistente
nella UI e' esattamente quello che farebbe un agente esterno. Ogni modifica
passa da ``Store``, compare nell'editor e si annulla con Ctrl+Z.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
from pathlib import Path
from typing import Any, Callable, Iterator

from . import llm
from . import mcp_server as ms

SYSTEM = """Sei l'assistente di ritocco dentro pedit, un editor di immagini a livelli
non distruttivo (tipo Photoshop). Lavori sul documento aperto: ogni tuo strumento e'
la stessa operazione dei pulsanti, compare subito nell'editor e si annulla con Ctrl+Z.

Regole:
- GUARDA: usa preview (grid=true per leggere le coordinate in pixel) prima di
  decidere posizioni e riquadri, e dopo un blocco di modifiche per controllarle.
- Misura prima di correggere (analyze_image), poi correggi poco: base (esposizione,
  bilanciamento) prima dello stile (apply_look).
- Non distruttivo: livelli di regolazione, maschere, toppe su livelli nuovi.
- batch per tante modifiche gia' decise: una chiamata, un undo.
- Prima di dire che hai finito: review_image.
- Non esportare mai di tua iniziativa: export_image solo se l'utente lo chiede.
- ai_status dice cosa c'e' (scontorno, ComfyUI): non promettere cio' che manca.

Rispondi nella lingua dell'utente, breve. Non ripetere cio' che l'utente vede gia'."""

# strumenti che dalla chat non hanno senso: l'interfaccia e' gia' aperta
_ESCLUSI = {"open_ui", "close_ui", "project_create", "project_open"}


class ChatUnavailable(RuntimeError):
    pass


def available() -> dict:
    return llm.stato()


def build_tools() -> list[dict]:
    """Schemi in formato Anthropic, presi dal server MCP (senza ``project``)."""
    out = []
    for nome in ms.STRUMENTI:
        if nome in _ESCLUSI:
            continue
        t = ms.mcp._tool_manager.get_tool(nome)
        if t is None:
            continue
        schema = json.loads(json.dumps(t.parameters))
        props = {k: v for k, v in (schema.get("properties") or {}).items()
                 if k not in ("project", "contesto_mcp")}
        req = [r for r in schema.get("required") or [] if r in props]
        s2 = {"type": "object", "properties": props}
        if req:
            s2["required"] = req
        if "$defs" in schema:
            s2["$defs"] = schema["$defs"]
        out.append({"name": nome, "description": (t.description or nome)[:1000], "input_schema": s2})
    return out


def execute(store: Any, name: str, args: dict) -> str | tuple[str, list[dict]]:
    """Esegue uno strumento sul documento aperto. Le immagini tornano a parte."""
    if name in _ESCLUSI or name not in ms.STRUMENTI:
        raise ValueError(f"unknown tool: {name}")
    ms._current[0] = store
    fn = ms.STRUMENTI[name]
    args = {k: v for k, v in (args or {}).items() if k != "project"}
    res = asyncio.run(fn(**args)) if inspect.iscoroutinefunction(fn) else fn(**args)
    if isinstance(res, list) and any(isinstance(x, ms.Image) for x in res):
        immagini, testi = [], []
        for x in res:
            if isinstance(x, ms.Image):
                immagini.append({"mime": "image/png", "b64": base64.b64encode(x.data).decode("ascii")})
            else:
                testi.append(x if isinstance(x, str) else ms._json(x))
        return "\n".join(testi), immagini
    return ms._json(res)


def _state_block(store: Any) -> str:
    s = store.summary()
    return "Stato del documento (livelli dall'alto):\n" + json.dumps(s, ensure_ascii=False, separators=(",", ":"))


def riferimenti(store: Any, refs: list[dict]) -> tuple[str, list[dict]]:
    """Zone indicate dall'utente sull'immagine: testo con le coordinate + l'immagine col riquadro."""
    from PIL import ImageDraw

    from . import render

    if not refs:
        return "", []
    righe, immagini = [], []
    p = store.project
    k = min(1.0, 1024 / max(p.width, 1))
    img = render.render(p, store.base_dir, scale=k).convert("RGB")
    d = ImageDraw.Draw(img)
    for i, r in enumerate(refs, 1):
        if r.get("type") == "area":
            x, y, w, h = r["box"]
            righe.append(f"[{i}] area x={x:.0f} y={y:.0f} w={w:.0f} h={h:.0f} (pixel del documento)")
            d.rectangle([x * k, y * k, (x + w) * k, (y + h) * k], outline=(255, 0, 0), width=3)
            d.text((x * k + 4, y * k + 4), str(i), fill=(255, 0, 0))
        elif r.get("type") == "layer":
            righe.append(f"[{i}] livello {r.get('id')} ({r.get('name', '')})")
        elif r.get("type") == "file":
            righe.append(f"[{i}] file allegato: {r.get('path')}")
            if Path(r.get("path", "")).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                try:
                    immagini.append(llm.immagine(r["path"]))
                except OSError:
                    pass
    if any(r.get("type") == "area" for r in refs):
        import io

        buf = io.BytesIO()
        img.save(buf, "PNG")
        immagini.insert(0, {"type": "image", "mime": "image/png", "path": "(riferimenti)",
                            "b64": base64.b64encode(buf.getvalue()).decode("ascii")})
    return "Riferimenti indicati dall'utente:\n" + "\n".join(righe), immagini


def run(store: Any, stato_chat: dict, question: str, refs: list[dict] | None = None,
        mcp_url: str | None = None, fermo: Callable[[], bool] = lambda: False) -> Iterator[dict]:
    """Un turno di chat: flusso di eventi text/tool/tool_done/tool_error/error/done."""
    try:
        p, cfg = llm.attivo()
    except RuntimeError as exc:
        yield {"type": "error", "message": str(exc)}
        return
    testo_rif, immagini = riferimenti(store, refs or [])
    contenuto: list[dict] = [*immagini, {"type": "text", "text": "\n\n".join(
        x for x in (_state_block(store), testo_rif, question) if x)}]
    # cambiando motore la cronologia non e' compatibile: si riparte
    if stato_chat.get("motore") != p["id"]:
        stato_chat.clear()
        stato_chat["motore"] = p["id"]
    history = stato_chat.setdefault("history", [])
    if p["tipo"] == "claude_code":
        if not mcp_url:
            yield {"type": "error", "message": "the editor's MCP server is not running"}
            return
        # Claude Code legge le immagini come file
        for c in contenuto:
            if c["type"] == "image" and c.get("path", "").startswith("("):
                f = store.assets_dir() / "chat_riferimenti.png"
                f.write_bytes(base64.b64decode(c["b64"]))
                c["path"] = str(f)
        yield from llm.run_claude_code(p, cfg, stato_chat, contenuto, mcp_url, store.base_dir, fermo)
        return
    tools = build_tools()
    run_op = lambda n, a: execute(store, n, a)  # noqa: E731
    if p["tipo"] == "anthropic":
        yield from llm.run_anthropic(p, cfg, SYSTEM, tools, history, contenuto, run_op, fermo)
    else:
        yield from llm.run_openai(p, cfg, SYSTEM, tools, history, contenuto, run_op, fermo)
