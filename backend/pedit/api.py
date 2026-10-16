"""API REST/WebSocket per l'interfaccia web, piu' il server MCP su ``/mcp``.

Nessuna modifica al documento nasce qui: ``/api/op/{nome}`` chiama il metodo
omonimo di ``Store``, lo stesso che chiamano l'agente MCP e l'assistente.
Qui ci sono solo trasporto, anteprime in cache e l'attesa delle risposte
dell'utente (conferma dell'export).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

from . import __version__, ai, analyze, llm, presets, render
from . import chat as chat_mod
from . import effects as fx
from .model import BLEND_MODES, IMAGE_EXTS, Project
from .store import PRESETS, EditError, Store


def _frontend_dir() -> Path:
    """UI compilata: dentro il pacchetto se installato da pip, altrimenti frontend/dist."""
    dentro = Path(__file__).resolve().parent / "webui"
    if (dentro / "index.html").is_file():
        return dentro
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


FRONTEND = _frontend_dir()

# operazioni di editing esposte alla UI (nomi dei metodi di Store)
OPS = {
    "set_document", "import_images", "remove_asset",
    "add_image", "add_text", "add_shape", "add_fill", "add_adjustment", "add_paint_layer",
    "paint_stroke", "remove_stroke", "set_layer", "set_transform", "align_layer", "set_text",
    "set_shape", "set_fill", "move_layer", "duplicate_layer", "remove_layer",
    "add_effect", "update_effect", "remove_effect", "move_effect", "apply_look",
    "set_mask", "update_mask", "remove_mask",
    "crop_canvas", "resize_canvas", "resize_image", "rasterize_layer", "merge_down", "flatten",
    "remove_background", "inpaint", "generate_image", "upscale", "auto_enhance", "match_color",
    "undo", "redo",
}


class Session:
    """Documento aperto + ascoltatori. Una sola sessione per processo."""

    def __init__(self) -> None:
        self.store: Store | None = None
        self.listeners: set[asyncio.Queue] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.lock = threading.RLock()
        self.chat: dict = {}
        self.fermo = threading.Event()
        self.domande: dict[str, dict] = {}
        self.porta: int | None = None
        self.seq = 0
        self._rev: tuple | None = None
        self._cache: dict[tuple, bytes] = {}

    def need(self) -> Store:
        if self.store is None:
            raise HTTPException(400, "no document open")
        return self.store

    def publish(self, event: dict) -> None:
        if event.get("type") == "project":
            self.seq += 1
            event = {**event, "seq": self.seq}
        if self.loop is None:
            return
        for q in list(self.listeners):
            self.loop.call_soon_threadsafe(q.put_nowait, event)

    def revision(self) -> str:
        """Impronta del contenuto: uno stato e il suo undo ritrovano le stesse anteprime."""
        st = self.store
        if st is None:
            return "0"
        r = self._rev
        if r and r[0] is st and r[1] is st.project and r[2] == st.version:
            return r[3]
        blob = json.dumps(st.project.to_dict(), sort_keys=True).encode()
        rev = hashlib.sha1(blob).hexdigest()[:16]
        self._rev = (st, st.project, st.version, rev)
        return rev

    def state(self) -> dict:
        st = self.store
        return {"project": st.summary("full") if st else None, "path": st.path if st else None,
                "revision": self.revision(), "seq": self.seq}

    def snapshot(self) -> tuple[Project, str | None]:
        """Copia del documento da renderizzare fuori dal lock."""
        with self.lock:
            st = self.need()
            return Project.from_obj(st.project.to_dict()), st.base_dir

    def cached(self, key: tuple, make) -> bytes:
        if key in self._cache:
            return self._cache[key]
        data = make()
        if len(self._cache) > 64:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = data
        return data


S = Session()


def attach(store: Store) -> None:
    """La UI lavora su questo Store (anche quello dell'agente MCP: open_ui)."""
    S.store = store
    store.on_change = lambda: S.publish({"type": "project"})
    S.chat.clear()
    S.publish({"type": "project"})


# --------------------------------------------------------------------------
# server MCP montato su /mcp
# --------------------------------------------------------------------------


class _PonteMcp:
    def __init__(self) -> None:
        self.app = None

    async def __call__(self, scope, receive, send):
        if self.app is None:
            await JSONResponse({"detail": "MCP server not running"}, status_code=503)(scope, receive, send)
            return
        await self.app(scope, receive, send)


_mcp = _PonteMcp()
MCP_ATTIVO = False


@asynccontextmanager
async def _vita(_app):
    global MCP_ATTIVO
    S.loop = asyncio.get_running_loop()
    try:
        from mcp.server.transport_security import TransportSecuritySettings

        from . import mcp_server

        sub = mcp_server.mcp.streamable_http_app(
            streamable_http_path="/", stateless_http=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    except Exception:  # noqa: BLE001 - senza MCP l'interfaccia funziona lo stesso
        sub = None
    if sub is None:
        yield
        return
    async with sub.router.lifespan_context(sub):
        _mcp.app = sub
        MCP_ATTIVO = True
        try:
            yield
        finally:
            MCP_ATTIVO = False
            _mcp.app = None


app = FastAPI(title="pedit", lifespan=_vita)
app.mount("/mcp", _mcp)

_host_consentiti: set[str] = set()
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _nome_host(valore: str) -> str:
    valore = valore.strip().lower()
    if valore.startswith("["):
        return valore.split("]")[0] + "]"
    return valore.rsplit(":", 1)[0] if valore.count(":") == 1 else valore


@app.middleware("http")
async def _solo_locale(request: Request, call_next):
    """L'API legge e scrive file ovunque: niente DNS rebinding, niente richieste da altri siti."""
    if _host_consentiti:
        if _nome_host(request.headers.get("host", "")) not in _host_consentiti:
            return JSONResponse({"detail": "non-local request refused"}, status_code=403)
        origin = request.headers.get("origin")
        if origin is not None and _nome_host(origin.split("://", 1)[-1]) not in _host_consentiti:
            return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
    return await call_next(request)


def mcp_url() -> str | None:
    if not MCP_ATTIVO or not S.porta:
        return None
    return f"http://127.0.0.1:{S.porta}/mcp/"


# --------------------------------------------------------------------------
# stato e documento
# --------------------------------------------------------------------------


def _cartella_immagini() -> str:
    home = Path.home()
    for nome in ("Pictures", "Immagini", "Images"):
        if (home / nome).is_dir():
            return str(home / nome)
    return str(home)


@app.get("/api/state")
def state() -> dict:
    return {
        **S.state(),
        "version": __version__,
        "home": _cartella_immagini(),
        "effects": fx.describe(),
        "looks": presets.describe(),
        "presets": {k: list(v) for k, v in PRESETS.items()},
        "blend_modes": list(BLEND_MODES),
        "fonts": ["sans", "serif", "mono", "display", "handwriting"],
        "chat": llm.stato(),
        "ai": ai.status(check_comfy=False),
        "mcp": mcp_url(),
    }


@app.get("/api/project")
def project_state() -> dict:
    return S.state()


class CreateBody(BaseModel):
    name: str = "untitled"
    preset: str | None = None
    width: int | None = None
    height: int | None = None
    background: str = "#ffffff"
    path: str | None = None
    from_image: str | None = None


class PathBody(BaseModel):
    path: str | None = None


@app.post("/api/project/create")
def project_create(body: CreateBody) -> dict:
    try:
        attach(Store.create(**body.model_dump()))
    except (EditError, OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return S.state()


@app.post("/api/project/open")
def project_open(body: PathBody) -> dict:
    if not body.path:
        raise HTTPException(400, "path missing")
    try:
        attach(Store.open(body.path))
    except (EditError, OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return S.state()


@app.post("/api/project/save")
def project_save(body: PathBody) -> dict:
    st = S.need()
    try:
        with S.lock:
            path = st.save(body.path)
    except (EditError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    S.publish({"type": "project"})
    return {"path": path, **S.state()}


def _plain(x: Any) -> Any:
    try:
        json.dumps(x)
        return x
    except TypeError:
        return str(x)


@app.post("/api/op/{name}")
def op(name: str, body: dict | None = None) -> dict:
    """Esegue un'operazione di Store e restituisce lo stato aggiornato."""
    if name not in OPS:
        raise HTTPException(404, f"unknown operation: {name}")
    st = S.need()
    try:
        with S.lock:
            result = getattr(st, name)(**dict(body or {}))
    except TypeError as exc:
        raise HTTPException(400, f"invalid arguments for {name}: {exc}") from exc
    except (EditError, ValueError, KeyError, FileNotFoundError) as exc:
        msg = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
        raise HTTPException(400, msg) from exc
    return {"result": _plain(result), **S.state()}


# --------------------------------------------------------------------------
# anteprime
# --------------------------------------------------------------------------


def _png(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.convert("RGB").save(buf, "JPEG", quality=88)
    else:
        img.save(buf, "PNG", compress_level=1)
    return buf.getvalue()


@app.get("/api/render")
def render_doc(w: int = 1200, rev: str = "") -> Response:
    """Il documento renderizzato, largo al massimo ``w`` (trasparenza conservata)."""
    w = max(32, min(int(w), 8000))
    snap, base = S.snapshot()
    key = ("doc", S.revision(), w)

    def fai() -> bytes:
        s = min(1.0, w / max(snap.width, 1))
        img = render.render(snap, base, scale=s, transparent=snap.background == "transparent")
        return _png(img)

    try:
        data = S.cached(key, fai)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(data, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/layer/{layer_id}/thumb")
def layer_thumb(layer_id: str, size: int = 64, rev: str = "") -> Response:
    snap, base = S.snapshot()
    key = ("thumb", S.revision(), layer_id, size)

    def fai() -> bytes:
        ly = snap.layer(layer_id)
        s = min(1.0, size * 2 / max(snap.width, snap.height, 1))
        if ly.type == "adjustment":
            img = Image.new("RGBA", (size, size), (90, 90, 110, 255))
        else:
            img = render.layer_image(snap, ly.id, base, scale=s)
            img.thumbnail((size, size))
        return _png(img)

    try:
        return Response(S.cached(key, fai), media_type="image/png")
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/asset/{asset_id}")
def asset_file(asset_id: str, size: int = 0):
    st = S.need()
    try:
        a = st.project.asset(asset_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    p = render.resolve(a.path, st.base_dir)
    if not p.is_file():
        raise HTTPException(404, "file missing")
    if size:
        img = render.load_image(p).copy()
        img.thumbnail((size, size))
        return Response(_png(img), media_type="image/png")
    return FileResponse(p)


@app.get("/api/analyze")
def analyze_doc(layer: str | None = None) -> dict:
    snap, base = S.snapshot()
    k = min(1.0, 1000 / max(snap.width, 1))
    if layer:
        arr = fx.to_np(render.layer_image(snap, layer, base, scale=k))
    else:
        arr = render.composite(snap, base, scale=k)
    return analyze.stats(arr)


@app.get("/api/review")
def review_doc() -> dict:
    snap, base = S.snapshot()
    w = analyze.review(snap, base)
    return {"ok": not w, "warnings": w}


@app.get("/api/ai")
def ai_status() -> dict:
    return ai.status()


# --------------------------------------------------------------------------
# file: sfoglia, importa, carica, esporta
# --------------------------------------------------------------------------


@app.get("/api/browse")
def browse(path: str | None = None) -> dict:
    p = Path(path or _cartella_immagini())
    if not p.is_dir():
        p = p.parent if p.parent.is_dir() else Path.home()
    voci = []
    try:
        for f in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if f.name.startswith("."):
                continue
            if f.is_dir():
                voci.append({"name": f.name, "path": str(f), "dir": True})
            elif f.suffix.lower() in IMAGE_EXTS or f.suffix.lower() == ".json":
                voci.append({"name": f.name, "path": str(f), "dir": False,
                             "kind": "doc" if f.suffix.lower() == ".json" else "image"})
    except OSError as exc:
        raise HTTPException(400, str(exc)) from exc
    radici = [f"{d}:\\" for d in "CDEFGH" if os.name == "nt" and Path(f"{d}:\\").exists()] or ["/"]
    return {"path": str(p.resolve()), "parent": str(p.resolve().parent), "entries": voci, "roots": radici}


class ImportBody(BaseModel):
    paths: list[str]
    add: bool = True


@app.post("/api/import")
def importa(body: ImportBody) -> dict:
    st = S.need()
    try:
        with S.lock, st.batch():
            for path in body.paths:
                if body.add:
                    st.add_image(path=path, name=Path(path).stem)
                else:
                    st.import_images([path])
    except (EditError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return S.state()


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...), add: str = Form("1")) -> dict:
    """File trascinati nel browser: copiati nella cartella degli asset (il browser non da' il percorso)."""
    st = S.need()
    dest = st.assets_dir() / "uploads"
    dest.mkdir(parents=True, exist_ok=True)
    salvati = []
    for f in files:
        nome = Path(f.filename or "image.png").name
        if Path(nome).suffix.lower() not in IMAGE_EXTS:
            continue
        target = dest / f"{uuid.uuid4().hex[:6]}_{nome}"
        with target.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        salvati.append(str(target))
    if not salvati:
        raise HTTPException(400, "no images in the upload")
    return await asyncio.to_thread(importa, ImportBody(paths=salvati, add=add == "1"))


class ExportBody(BaseModel):
    path: str
    quality: int = 92
    scale: float = 1.0
    transparent: bool = False


@app.post("/api/export")
def export(body: ExportBody) -> dict:
    st = S.need()
    try:
        return st.export_image(body.path, body.quality, body.scale, body.transparent)
    except (EditError, OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


# --------------------------------------------------------------------------
# domande all'utente (conferma export dall'agente)
# --------------------------------------------------------------------------


def conferma_export(path: str, timeout: float = 600) -> bool:
    """L'agente vuole scrivere un file: lo decide l'utente, nel browser."""
    if not S.listeners:
        return False
    qid = uuid.uuid4().hex[:10]
    ev = threading.Event()
    S.domande[qid] = {"event": ev, "answer": False}
    S.publish({"type": "question", "id": qid, "kind": "export",
               "text": f"The assistant wants to export the image to {path}. Allow?"})
    ev.wait(timeout)
    return bool(S.domande.pop(qid, {}).get("answer"))


class AnswerBody(BaseModel):
    id: str
    ok: bool


@app.post("/api/answer")
def answer(body: AnswerBody) -> dict:
    d = S.domande.get(body.id)
    if d is None:
        raise HTTPException(404, "no such question")
    d["answer"] = body.ok
    d["event"].set()
    return {"ok": True}


# --------------------------------------------------------------------------
# assistente
# --------------------------------------------------------------------------


@app.get("/api/llm")
def llm_stato() -> dict:
    return llm.stato()


class LlmBody(BaseModel):
    provider: str
    key: str | None = None
    model: str | None = None
    url: str | None = None


@app.post("/api/llm")
def llm_imposta(body: LlmBody) -> dict:
    try:
        return llm.imposta(body.provider, body.key, body.model, body.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class ChatBody(BaseModel):
    question: str
    refs: list[dict] = []


@app.post("/api/chat")
def chat(body: ChatBody) -> StreamingResponse:
    st = S.need()
    S.fermo.clear()

    def flusso():
        try:
            for ev in chat_mod.run(st, S.chat, body.question, body.refs, mcp_url(), S.fermo.is_set):
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        except Exception as exc:  # noqa: BLE001 - l'errore va nella chat, non nel log del server
            yield json.dumps({"type": "error", "message": str(exc)}) + "\n"

    return StreamingResponse(flusso(), media_type="application/x-ndjson")


@app.post("/api/chat/stop")
def chat_stop() -> dict:
    S.fermo.set()
    return {"ok": True}


@app.post("/api/chat/reset")
def chat_reset() -> dict:
    S.chat.clear()
    return {"ok": True}


# --------------------------------------------------------------------------
# eventi
# --------------------------------------------------------------------------


@app.websocket("/ws")
async def ws(socket: WebSocket) -> None:
    if _host_consentiti and _nome_host(socket.headers.get("host", "")) not in _host_consentiti:
        await socket.close(code=1008)
        return
    await socket.accept()
    S.loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    S.listeners.add(q)
    try:
        while True:
            ev = await q.get()
            await socket.send_json(ev)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        S.listeners.discard(q)


# interfaccia compilata (montata per ultima: le rotte /api vincono)
if FRONTEND.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="ui")
else:
    @app.get("/")
    def _senza_ui() -> JSONResponse:
        return JSONResponse({"detail": "web UI not built: cd frontend && npm ci && npm run build"})


# --------------------------------------------------------------------------
# avvio
# --------------------------------------------------------------------------

_bg: dict[str, Any] = {}


def _consenti(host: str) -> None:
    _host_consentiti.update(_LOOPBACK)
    if host not in ("0.0.0.0", "::", ""):
        _host_consentiti.add(host)
    else:
        _host_consentiti.clear()


def serve(store: Store | None = None, host: str = "127.0.0.1", port: int = 8770, browser: bool = True) -> None:
    import uvicorn

    if store is not None:
        attach(store)
    _consenti(host)
    S.porta = port
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    if browser:
        threading.Timer(1.0, lambda: __import__("webbrowser").open(url)).start()
    print(f"pedit {__version__} - {url}  (MCP: {url}/mcp/)")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def background_info() -> dict | None:
    return _bg.get("info")


def serve_background(store: Store, port: int = 8770) -> str:
    """Interfaccia nello stesso processo del server MCP: stesso Store, aggiornamenti dal vivo."""
    import uvicorn

    attach(store)
    if _bg.get("info"):
        return _bg["info"]["url"]
    _consenti("127.0.0.1")
    S.porta = port
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}"
    _bg["info"] = {"url": url}
    _bg["server"] = server
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    return url


def stop_background() -> bool:
    server = _bg.pop("server", None)
    _bg.pop("info", None)
    if server is None:
        return False
    server.should_exit = True
    return True
