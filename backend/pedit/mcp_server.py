"""Server MCP: espone l'editor come strumenti per un agente.

Filosofia degli strumenti (la stessa di vedit):
- ogni strumento e' un metodo di ``Store``: niente scorciatoie riservate
  all'agente, niente che la UI non possa fare;
- risposte compatte, in JSON senza spazi: ogni risposta resta nel contesto;
- errori con messaggio utile, che dicono cosa era ammesso;
- ``preview`` restituisce l'immagine vera, cosi' l'agente *guarda* il
  risultato invece di indovinarlo.

Avvio: ``pedit-mcp`` (stdio). Dentro ``pedit ui`` lo stesso server risponde su
``/mcp/`` e lavora sul documento aperto nel browser.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import io
import json
import sys
import time as _time
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image as PILImage
from PIL import ImageDraw

from . import __version__, ai, analyze, cloud, comfy, presets, render, segnalazioni
from . import effects as fx
from .model import BLEND_MODES
from .store import PRESETS, EditError, Store

ISTRUZIONI = """\
Editor di immagini a livelli (tipo Photoshop/GIMP), non distruttivo.

Flusso: project_create (o project_open su una foto) -> add_image / add_text /
add_shape / add_fill -> regolazioni (add_adjustment, apply_look, auto_enhance) ->
ritocco (add_effect, set_mask, inpaint, remove_background) -> preview per
guardare -> review_image -> export_image (solo se l'utente lo chiede).

Coordinate in pixel del documento, origine in alto a sinistra. x/y di un
livello sono l'angolo in alto a sinistra del contenuto; rotation in gradi,
orario. I livelli in project_info vanno dall'alto (disegnato sopra) al basso.
Un livello si indica con l'id o con il nome.

NIENTE E' DISTRUTTIVO, e deve restare cosi':
- i filtri sono voci in effects, modificabili e spegnibili (update_effect);
- per correggere tutta l'immagine usa add_adjustment / apply_look (un livello
  di regolazione in cima), non un effetto su ogni livello;
- per nascondere una parte usa set_mask, non cancellare pixel;
- remove_background mette una maschera, inpaint mette una toppa su un livello
  nuovo: l'originale resta intatto e l'utente puo' tornare indietro.

== REGOLA PRIMA: GUARDA ==
preview mostra il documento vero (grid=true disegna una griglia con le
coordinate in pixel: usala prima di decidere posizioni e riquadri). Con box
ingrandisci un dettaglio. Un ritocco non guardato e' un ritocco non fatto.
analyze_image da' i numeri (esposizione, pixel bruciati, dominante, nitidezza):
misura prima di correggere, non correggere "a occhio" una foto gia' bruciata.

== COME RITOCCARE ==
1. Prima la base, poi lo stile: esposizione e bilanciamento (auto_enhance o
   levels/temperature), POI il look. Un look su una foto storta resta storto.
2. Poco e' meglio: saturazione oltre +0.4, contrasto oltre +0.5, nitidezza
   oltre 1.5 si vedono come difetti. Controlla con preview a box ingrandito.
3. Testo leggibile: contrasto con lo sfondo (review_image lo misura), margini
   dai bordi (align_layer con margin), dimensione proporzionata alla tela.
   Su una foto movimentata: stroke o drop_shadow, o una forma dietro.
4. Composizione: allinea con align_layer invece di calcolare a mano;
   terzi e margini coerenti valgono piu' di qualunque effetto.
5. IA: remove_background (soggetto), inpaint senza prompt per togliere
   oggetti (o con points = pennello correttivo), inpaint con prompt /
   generate_image / edit_image (image-to-image) / expand_canvas / upscale con
   ComfyUI o con un servizio in rete (backend=openai, gemini, stability,
   openrouter, replicate...). describe_image fa image-to-text. ai_status dice
   cosa e' disponibile qui: chiedilo prima di promettere. I servizi in rete
   costano all'utente: backend="auto" li usa solo se ne ha scelto uno come
   predefinito; nominarne uno esplicitamente solo se l'utente lo chiede.
6. Livelli come in Photoshop: group_layers / move_layer(into=gruppo) per i
   gruppi, set_layer(clip=true) per la maschera di ritaglio sul livello sotto,
   mask_stroke per dipingere una maschera (hide/reveal, anche per rifinire uno
   scontorno), paint_stroke(source=[x,y]) e' il timbro clone (heal=true il
   pennello correttivo).

ECONOMIA. Ogni chiamata rilegge tutto il contesto. batch applica una lista di
modifiche in un colpo solo (un undo), e con look=true restituisce anche
l'anteprima. project_info solo se non conosci lo stato.

Non esportare mai di tua iniziativa: export_image solo se l'utente lo chiede.
Se l'utente vuole vedere o correggere a mano, open_ui apre l'editor sullo
stesso documento. Quando qualcosa non va (errore, strumento scomodo, funzione
che manca) annotalo con report_issue e prosegui.
"""

mcp = MCPServer(name="pedit", version=__version__, instructions=ISTRUZIONI)

_PREVISTI = (ValueError, KeyError, FileNotFoundError)


def _messaggio(exc: BaseException) -> str:
    return str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)


def _arrotonda(x):
    if isinstance(x, float):
        return round(x, 3)
    if isinstance(x, dict):
        return {k: _arrotonda(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_arrotonda(v) for v in x]
    return x


def _json(x) -> str:
    return json.dumps(_arrotonda(x), ensure_ascii=False, separators=(",", ":"), default=str)


def _compatto(res):
    if res is None or isinstance(res, (str, Image)):
        return res
    if isinstance(res, (list, tuple)) and any(isinstance(x, Image) for x in res):
        return [x if isinstance(x, (str, Image)) else _json(x) for x in res]
    return _json(res)


def _snello(schema: Any) -> Any:
    """Schema senza i ``title`` ripetuti e gli ``anyOf [x, null]`` di pydantic."""
    if isinstance(schema, list):
        return [_snello(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: _snello(v) for k, v in schema.items() if k != "title" and not (k == "default" and v is None)}
    varianti = out.get("anyOf")
    if isinstance(varianti, list):
        senza_null = [v for v in varianti if v != {"type": "null"}]
        if len(senza_null) == 1 and isinstance(senza_null[0], dict):
            del out["anyOf"]
            out = {**senza_null[0], **out}
        elif len(senza_null) != len(varianti):
            out["anyOf"] = senza_null
    return out


# nome -> funzione, per ``batch`` e per l'assistente in chat
STRUMENTI: dict[str, Any] = {}
# strumenti che non hanno senso dentro batch (anteprime, export, interfaccia)
_FUORI_BATCH = {"batch", "preview", "export_image", "open_ui", "close_ui", "undo", "redo",
                "project_create", "project_open", "report_issue", "ai_status", "ai_services", "describe_image"}

_CTX: contextvars.ContextVar[Context | None] = contextvars.ContextVar("pedit_ctx", default=None)
BATTITO_S = 15.0


def _strumento(*args, **kwargs):
    registra = mcp.tool(*args, structured_output=False, **kwargs)

    def applica(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def avvolta(*a, contesto_mcp=None, **k):
                _CTX.set(contesto_mcp)
                try:
                    return _compatto(await fn(*a, **k))
                except _PREVISTI as exc:
                    raise ToolError(_messaggio(exc)) from exc
        else:
            @functools.wraps(fn)
            def avvolta(*a, contesto_mcp=None, **k):
                try:
                    return _compatto(fn(*a, **k))
                except _PREVISTI as exc:
                    raise ToolError(_messaggio(exc)) from exc
        firma = inspect.signature(fn)
        avvolta.__signature__ = firma.replace(parameters=[
            *firma.parameters.values(),
            inspect.Parameter("contesto_mcp", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Context)])
        avvolta.__annotations__ = {**getattr(fn, "__annotations__", {}), "contesto_mcp": Context}
        registra(avvolta)
        nome = kwargs.get("name") or fn.__name__
        STRUMENTI[nome] = fn
        tool = mcp._tool_manager.get_tool(nome)
        if tool is not None:
            tool.parameters = _snello(tool.parameters)
            tool.description = inspect.cleandoc(tool.description or "")
        return fn

    return applica


async def _off(fn, *args, **kwargs):
    """Lavoro bloccante (render grandi, IA) fuori dal loop, con battiti di progresso.

    Claude Code interrompe una chiamata muta per 300 secondi: una generazione
    con ComfyUI su una GPU piccola ci arriva.
    """
    ctx = _CTX.get()
    if ctx is None:
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
    t0, esito = _time.monotonic(), []

    async def battito():
        while True:
            await anyio.sleep(BATTITO_S)
            try:
                await ctx.report_progress(int(_time.monotonic() - t0), None, "working")
            except Exception:  # noqa: BLE001
                return

    async with anyio.create_task_group() as tg:
        tg.start_soon(battito)
        try:
            esito.append(await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs)))
        finally:
            tg.cancel_scope.cancel()
    return esito[0]


# --------------------------------------------------------------------------
# documento corrente
# --------------------------------------------------------------------------

_stores: dict[str, Store] = {}
_current: list[Store | None] = [None]


def _ospite() -> Store | None:
    """Il documento aperto nell'interfaccia, se questo server gira dentro di lei."""
    api = sys.modules.get("pedit.api")
    if api is not None and api.MCP_ATTIVO and api.S.store is not None:
        return api.S.store
    return None


def _ui_attach(s: Store) -> None:
    api = sys.modules.get("pedit.api")
    if api is not None and (api.background_info() or api.MCP_ATTIVO) and api.S.store is not s:
        api.attach(s)


def _store(project: str | None = None) -> Store:
    if project is None and (ospite := _ospite()) is not None:
        _current[0] = ospite
        return ospite
    if project:
        key = str(Path(project).resolve())
        if key not in _stores:
            _stores[key] = Store.open(key)
        _current[0] = _stores[key]
        _ui_attach(_current[0])
        return _current[0]
    if _current[0] is None:
        raise EditError("no document open: use project_create or project_open")
    return _current[0]


def _png(img: PILImage.Image) -> Image:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=False)
    return Image(data=buf.getvalue(), format="png")


def _griglia(img: PILImage.Image, W: int, H: int, box: list[float] | None = None) -> PILImage.Image:
    """Griglia al 10% con le coordinate in pixel del documento: per leggere posizioni."""
    img = img.convert("RGB")
    d = ImageDraw.Draw(img)
    x0, y0, bw, bh = box or [0, 0, W, H]
    kx, ky = img.width / bw, img.height / bh
    for i in range(1, 10):
        px = x0 + bw * i / 10
        py = y0 + bh * i / 10
        X, Y = (px - x0) * kx, (py - y0) * ky
        d.line([(X, 0), (X, img.height)], fill=(255, 0, 255), width=1)
        d.line([(0, Y), (img.width, Y)], fill=(255, 0, 255), width=1)
        for (tx, ty, t) in ((X + 2, 2, f"{px:.0f}"), (2, Y + 2, f"{py:.0f}")):
            d.rectangle([tx - 1, ty - 1, tx + 7 * len(t), ty + 11], fill=(0, 0, 0))
            d.text((tx, ty), t, fill=(255, 255, 255))
    return img


# --------------------------------------------------------------------------
# documento
# --------------------------------------------------------------------------


@_strumento()
def project_create(path: str | None = None, name: str = "untitled", preset: str | None = None,
                   width: int | None = None, height: int | None = None, background: str = "#ffffff",
                   from_image: str | None = None) -> dict:
    """Create a document. preset: 1080p, 4k, square, instagram_post, story, youtube_thumbnail,
    a4_300dpi, a4_150dpi, web_banner, og_image; or width/height; or from_image=<file> to start
    from a photo (canvas = photo size). path=<file.json> saves it (autosave after every edit).
    background can be 'transparent'."""
    s = Store.create(name=name, preset=preset, width=width, height=height, background=background,
                     path=path, from_image=from_image)
    if path:
        _stores[s.path] = s
    _current[0] = s
    _ui_attach(s)
    return s.summary()


@_strumento()
def project_open(path: str) -> dict:
    """Open a pedit .json document, or a photo (becomes a new unsaved document of its size)."""
    s = Store.open(path)
    if s.path:
        _stores[s.path] = s
    _current[0] = s
    _ui_attach(s)
    return s.summary()


@_strumento()
def project_info(detail: str = "normal", project: str | None = None) -> dict:
    """Document state: size, layers (top first) with ids, boxes, effects, masks. detail='full' adds all params."""
    return _store(project).summary(detail)


@_strumento()
def project_save(path: str | None = None, project: str | None = None) -> dict:
    """Save the document (.json). With path: save as."""
    return {"path": _store(project).save(path)}


@_strumento()
def project_settings(name: str | None = None, background: str | None = None, dpi: int | None = None,
                     project: str | None = None) -> dict:
    """Document name, background color ('transparent' allowed), dpi."""
    return _store(project).set_document(name, background, dpi)


@_strumento()
def undo(project: str | None = None) -> dict:
    """Undo the last edit."""
    return _store(project).undo()


@_strumento()
def redo(project: str | None = None) -> dict:
    """Redo the last undone edit."""
    return _store(project).redo()


# --------------------------------------------------------------------------
# livelli
# --------------------------------------------------------------------------


@_strumento()
def add_image(path: str | None = None, asset: str | None = None, x: float | None = None,
              y: float | None = None, scale: float | None = None, fit: str = "contain",
              name: str | None = None, above: str | None = None, project: str | None = None) -> dict:
    """Image layer from a file (path) or an imported asset. fit: contain, cover, width, height, none
    (ignored if scale given). Without x/y it is centered. above=<layer> to insert right above it."""
    return _store(project).add_image(asset=asset, path=path, x=x, y=y, scale=scale, fit=fit, name=name, above=above)


@_strumento()
def add_text(text: str, x: float | None = None, y: float | None = None, size: float = 64,
             color: str = "#ffffff", font: str = "sans", bold: bool = False, italic: bool = False,
             align: str = "left", line_spacing: float = 1.2, stroke_width: float = 0,
             stroke_color: str = "#000000", max_width: float = 0, name: str | None = None,
             project: str | None = None) -> dict:
    """Text layer. font: sans, serif, mono, display, handwriting, or a system font name/file.
    max_width>0 wraps lines. Without x/y it is centered; then align_layer to place it."""
    return _store(project).add_text(text, x, y, size, color, font, bold, italic, align, line_spacing,
                                    stroke_width, stroke_color, max_width, name)


@_strumento()
def add_shape(kind: str = "rect", x: float = 0, y: float = 0, w: float = 200, h: float = 200,
              fill: str | None = "#ffffff", stroke: str | None = None, stroke_width: float = 0,
              radius: float = 0, points: list | None = None, name: str | None = None,
              project: str | None = None) -> dict:
    """Vector shape: rect (radius = rounded corners), ellipse, line, polygon (points relative to x,y).
    Colors accept #rrggbbaa for transparency."""
    return _store(project).add_shape(kind, x, y, w, h, fill, stroke, stroke_width, radius, points, name)


@_strumento()
def add_fill(kind: str = "solid", color: str = "#000000", color2: str = "#ffffff", angle: float = 90,
             cx: float = 0.5, cy: float = 0.5, name: str | None = None, index: int | None = None,
             project: str | None = None) -> dict:
    """Full-canvas fill: solid, linear gradient (angle 90 = left->right, 180 = top->bottom) or
    radial (center cx, cy as fractions). index=0 puts it at the bottom (a background)."""
    return _store(project).add_fill(kind, color, color2, angle, cx, cy, name, index)


@_strumento()
def add_adjustment(type: str | None = None, params: dict | None = None, effects: list[dict] | None = None,
                   name: str | None = None, project: str | None = None) -> dict:
    """Adjustment layer on top: its filters change everything below it, non-destructively.
    One filter (type + params) or a chain effects=[{type, params}, ...]. See list_effects."""
    return _store(project).add_adjustment(type, params, effects, name)


@_strumento()
def paint_stroke(points: list, layer: str | None = None, color: str = "#000000", size: float = 12,
                 hardness: float = 0.8, opacity: float = 1.0, erase: bool = False, source: list | None = None,
                 heal: bool = False, project: str | None = None) -> dict:
    """Brush stroke: polyline [[x,y],...] in document px on a paint layer (created if missing).
    erase=true erases previous strokes of that layer. Clone stamp: source=[x,y] is where to copy from
    for the first point (copies everything below the layer); heal=true = healing brush (matches the
    light and color of the destination)."""
    return _store(project).paint_stroke(points, layer, color, size, hardness, opacity, erase, source=source,
                                        heal=heal)


@_strumento()
def set_layer(layer: str, name: str | None = None, visible: bool | None = None, locked: bool | None = None,
              opacity: float | None = None, blend: str | None = None, clip: bool | None = None,
              project: str | None = None) -> dict:
    """Layer name, visibility, lock, opacity 0..1, clip (clipping mask onto the layer below), blend mode:
    normal, dissolve, darken, multiply, color_burn, linear_burn, darker_color, lighten, screen,
    color_dodge, add (linear dodge), lighter_color, overlay, soft_light, hard_light, vivid_light,
    linear_light, pin_light, hard_mix, difference, exclusion, subtract, divide, hue, saturation, color,
    luminosity; groups also pass_through."""
    return _store(project).set_layer(layer, name, visible, locked, opacity, blend, clip)


@_strumento()
def set_transform(layer: str, x: float | None = None, y: float | None = None, scale: float | None = None,
                  rotation: float | None = None, flip_h: bool | None = None, flip_v: bool | None = None,
                  crop: list | None = None, width: float | None = None, height: float | None = None,
                  project: str | None = None) -> dict:
    """Move/scale/rotate (degrees, clockwise)/flip a layer. width or height = target size on the
    canvas (keeps proportions). crop=[x,y,w,h] in SOURCE pixels of an image layer ([] removes it)."""
    return _store(project).set_transform(layer, x, y, scale, rotation, flip_h, flip_v, crop, width, height)


@_strumento()
def align_layer(layer: str, h: str | None = "center", v: str | None = "middle", margin: float = 0,
                project: str | None = None) -> dict:
    """Align to the canvas: h left/center/right, v top/middle/bottom (null = leave), margin in px."""
    return _store(project).align_layer(layer, h, v, margin)


@_strumento()
def set_text(layer: str, text: str | None = None, font: str | None = None, size: float | None = None,
             color: str | None = None, bold: bool | None = None, italic: bool | None = None,
             align: str | None = None, line_spacing: float | None = None, stroke_width: float | None = None,
             stroke_color: str | None = None, max_width: float | None = None,
             project: str | None = None) -> dict:
    """Change a text layer's text or style."""
    return _store(project).set_text(layer, text, font=font, size=size, color=color, bold=bold, italic=italic,
                                    align=align, line_spacing=line_spacing, stroke_width=stroke_width,
                                    stroke_color=stroke_color, max_width=max_width)


@_strumento()
def set_shape(layer: str, kind: str | None = None, w: float | None = None, h: float | None = None,
              fill: str | None = None, stroke: str | None = None, stroke_width: float | None = None,
              radius: float | None = None, points: list | None = None, project: str | None = None) -> dict:
    """Change a shape layer (fill/stroke 'none' removes them)."""
    return _store(project).set_shape(layer, kind=kind, w=w, h=h, fill=fill, stroke=stroke,
                                     stroke_width=stroke_width, radius=radius, points=points)


@_strumento()
def set_fill(layer: str, kind: str | None = None, color: str | None = None, color2: str | None = None,
             angle: float | None = None, cx: float | None = None, cy: float | None = None,
             project: str | None = None) -> dict:
    """Change a fill/gradient layer."""
    return _store(project).set_fill(layer, kind=kind, color=color, color2=color2, angle=angle, cx=cx, cy=cy)


@_strumento()
def move_layer(layer: str, to: str | None = None, index: int | None = None, into: str | None = None,
               project: str | None = None) -> dict:
    """Reorder within its list: to=up/down/top/bottom, or index (0 = bottom). into=<group> moves it
    inside a group; into="root" takes it out of its group."""
    return _store(project).move_layer(layer, index, to, into)


@_strumento()
def group_layers(layers: list[str], name: str | None = None, project: str | None = None) -> dict:
    """Put layers (same level) into a new group. Groups have their own opacity, blend (default
    pass_through), mask and effects; an adjustment inside a non pass-through group affects only the group."""
    return _store(project).group_layers(layers, name)


@_strumento()
def ungroup(layer: str, project: str | None = None) -> dict:
    """Dissolve a group: its layers go back in its place."""
    return _store(project).ungroup(layer)


@_strumento()
def duplicate_layer(layer: str, name: str | None = None, project: str | None = None) -> dict:
    """Copy a layer (placed right above it)."""
    return _store(project).duplicate_layer(layer, name)


@_strumento()
def remove_layer(layer: str, project: str | None = None) -> dict:
    """Delete a layer (undoable)."""
    return _store(project).remove_layer(layer)


@_strumento()
def rasterize_layer(layer: str, project: str | None = None) -> dict:
    """Turn a text/shape/paint layer (with its effects and mask) into pixels."""
    return _store(project).rasterize_layer(layer)


@_strumento()
def merge_down(layer: str, project: str | None = None) -> dict:
    """Merge a layer into the one below."""
    return _store(project).merge_down(layer)


@_strumento()
def flatten(project: str | None = None) -> dict:
    """All visible layers into one image layer."""
    return _store(project).flatten()


# --------------------------------------------------------------------------
# effetti e maschere
# --------------------------------------------------------------------------


@_strumento()
def list_effects(name: str | None = None) -> list | dict:
    """Effect catalogue. Without name: names and one line each. With name: its parameters."""
    if name:
        sp = fx.spec(name)
        return {"name": name, "kind": sp["kind"], "desc": sp["desc"],
                "params": {k: {kk: vv for kk, vv in v.items() if kk != "px" or vv} for k, v in sp["params"].items()}}
    return [f"{e['name']} ({e['kind']}): {e['desc']}" for e in fx.describe()]


@_strumento()
def add_effect(type: str, layer: str, params: dict | None = None, index: int | None = None,
               project: str | None = None) -> dict:
    """Filter or layer style on one layer (filters before the mask, styles after: shadow, stroke, glow)."""
    return _store(project).add_effect(type, layer, params, index)


@_strumento()
def update_effect(layer: str, index: int, params: dict | None = None, enabled: bool | None = None,
                  project: str | None = None) -> dict:
    """Change an effect's params or switch it off/on. index = position in the layer's effects."""
    return _store(project).update_effect(layer, index, params, enabled)


@_strumento()
def remove_effect(layer: str, index: int, project: str | None = None) -> dict:
    """Remove an effect from a layer."""
    return _store(project).remove_effect(layer, index)


@_strumento()
def move_effect(layer: str, index: int, to: int, project: str | None = None) -> dict:
    """Reorder effects (order matters: denoise before sharpen)."""
    return _store(project).move_effect(layer, index, to)


@_strumento()
def list_looks() -> list:
    """Ready-made looks for apply_look."""
    return [f"{x['name']}: {x['desc']}" for x in presets.describe()]


@_strumento()
def apply_look(name: str, layer: str | None = None, project: str | None = None) -> dict:
    """Apply a look: as an adjustment layer on top (default) or as effects on one layer."""
    return _store(project).apply_look(name, layer)


@_strumento()
def set_mask(layer: str, kind: str = "rect", box: list | None = None, path: str | None = None,
             invert: bool = False, feather: float = 0, density: float = 1.0, hide_all: bool = False,
             project: str | None = None) -> dict:
    """Layer mask (white = visible). kind rect/ellipse with box=[x,y,w,h] in document px, image
    with path (grayscale or alpha file, aligned to the layer content), or paint (to paint with
    mask_stroke; hide_all=true starts fully hidden). feather = soft edge px."""
    return _store(project).set_mask(layer, kind, box, None, path, invert, feather, density, hide_all)


@_strumento()
def mask_stroke(layer: str, points: list, mode: str = "hide", size: float = 40, hardness: float = 0.6,
                opacity: float = 1.0, project: str | None = None) -> dict:
    """Paint on a layer's mask: mode hide or reveal, polyline [[x,y],...] in document px. Works on any
    mask (refine an AI cut-out); a layer without a mask gets a paint mask."""
    return _store(project).mask_stroke(layer, points, mode, size, hardness, opacity)


@_strumento()
def update_mask(layer: str, invert: bool | None = None, feather: float | None = None,
                density: float | None = None, box: list | None = None, project: str | None = None) -> dict:
    """Invert, soften, weaken or move an existing mask."""
    return _store(project).update_mask(layer, invert, feather, density, box)


@_strumento()
def remove_mask(layer: str, project: str | None = None) -> dict:
    """Remove a layer's mask."""
    return _store(project).remove_mask(layer)


# --------------------------------------------------------------------------
# tela
# --------------------------------------------------------------------------


@_strumento()
def crop_canvas(x: float, y: float, width: float, height: float, project: str | None = None) -> dict:
    """Crop the canvas to a box (document px). Layers are kept whole: undoable without loss."""
    return _store(project).crop_canvas(x, y, width, height)


@_strumento()
def resize_canvas(width: int, height: int, anchor: str = "center", project: str | None = None) -> dict:
    """Change canvas size without scaling content. anchor: center, top, bottom_left, ..."""
    return _store(project).resize_canvas(width, height, anchor)


@_strumento()
async def expand_canvas(left: float = 0, top: float = 0, right: float = 0, bottom: float = 0,
                        prompt: str | None = None, backend: str = "auto", seed: int | None = None,
                        project: str | None = None) -> dict:
    """Generative expand: add px on each side and fill the new borders (a new top layer masked to them).
    No prompt = classic fill (fine for sky, walls, plain backgrounds); with prompt or an AI service =
    generative."""
    s = _store(project)
    return await _off(s.expand_canvas, left, top, right, bottom, prompt, backend, seed)


@_strumento()
def resize_image(width: int | None = None, height: int | None = None, project: str | None = None) -> dict:
    """Scale the whole document (canvas and content), proportions kept: pass one side."""
    return _store(project).resize_image(width, height)


# --------------------------------------------------------------------------
# guardare e misurare
# --------------------------------------------------------------------------


@_strumento()
async def preview(width: int = 1024, box: list | None = None, layer: str | None = None, grid: bool = False,
                  project: str | None = None) -> list:
    """LOOK at the result: the real rendered image. box=[x,y,w,h] zooms on a detail (document px).
    layer=<id> shows that layer alone. grid=true overlays a 10% grid labelled in document px."""
    s = _store(project)
    width = max(64, min(int(width), 2048))
    p = s.project

    def fai():
        if box is not None:
            x, y, w, h = s._check_box(box)
            k = min(4.0, width / w)
            if layer:
                img = render.layer_image(p, s._layer(layer, edit=False).id, s.base_dir, scale=k)
            else:
                img = render.render(p, s.base_dir, scale=k)
            img = img.crop((int(x * k), int(y * k), int((x + w) * k), int((y + h) * k)))
        else:
            k = min(1.0, width / p.width)
            if layer:
                img = render.layer_image(p, s._layer(layer, edit=False).id, s.base_dir, scale=k)
                bg = PILImage.new("RGBA", img.size, (128, 128, 128, 255))
                img = PILImage.alpha_composite(bg, img)
            else:
                img = render.render(p, s.base_dir, scale=k)
        if grid:
            img = _griglia(img, p.width, p.height, s._check_box(box) if box else None)
        return img

    img = await _off(fai)
    return [_png(img), f"{p.width}x{p.height} document, shown at {img.width}x{img.height}"]


@_strumento()
async def analyze_image(layer: str | None = None, box: list | None = None, project: str | None = None) -> dict:
    """Numbers about the image (or one layer, or a box): brightness, contrast, clipped shadows/
    highlights, saturation, color cast, sharpness, dominant colors. Measure before correcting."""
    s = _store(project)

    def fai():
        k = min(1.0, 1200 / max(s.project.width, 1))
        if layer:
            arr = fx.to_np(render.layer_image(s.project, s._layer(layer, edit=False).id, s.base_dir, scale=k))
        else:
            arr = render.composite(s.project, s.base_dir, scale=k)
        if box:
            x, y, w, h = s._check_box(box)
            arr = arr[int(y * k):int((y + h) * k), int(x * k):int((x + w) * k)]
        return analyze.stats(arr)

    return await _off(fai)


@_strumento()
async def review_image(project: str | None = None) -> dict:
    """Check the document: text off-canvas or with low contrast, layers outside, blown highlights,
    upscaled images... each warning names the tool that fixes it. Not done while it warns."""
    s = _store(project)
    avvisi = await _off(analyze.review, s.project, s.base_dir)
    return {"ok": not avvisi, "warnings": avvisi}


# --------------------------------------------------------------------------
# IA
# --------------------------------------------------------------------------


@_strumento()
async def ai_status() -> dict:
    """What AI is available here: background removal, inpaint backend, ComfyUI (generation, upscale),
    online services with a key (online.ready: service -> tasks) and the default one."""
    return await _off(ai.status)


@_strumento()
def ai_services(service: str | None = None, models: dict | None = None, url: str | None = None,
                default: bool | None = None) -> dict:
    """Online AI services (OpenAI, Gemini, Stability, OpenRouter, Replicate, Anthropic, Ollama, custom):
    which are ready, for which tasks (generate, edit, inpaint, describe, upscale, cutout), with which
    models. With service: set its models {task: model}, url, or default=true (used by backend="auto").
    API keys are pasted by the user in the editor (AI models) or set as env vars, never here."""
    if service:
        return cloud.imposta(service, None, models, url, default)
    st = cloud.stato()
    return {"default": st["default"] or None,
            "services": [{k: x[k] for k in ("id", "pronto", "fa", "modelli")} | ({"why": x["motivo"]} if x["motivo"] else {})
                         for x in st["providers"]]}


@_strumento()
async def remove_background(layer: str, keep: str = "subject", model: str = "isnet-general-use",
                            backend: str = "local", project: str | None = None) -> dict:
    """AI cut-out as a layer MASK (the photo stays intact). keep=background inverts it.
    Models: isnet-general-use, u2net, u2net_human_seg (people), birefnet-general (best, slow).
    backend=stability uses the online service instead of the local model."""
    s = _store(project)
    return await _off(s.remove_background, layer, keep, model, backend)


@_strumento()
async def subject_box(layer: str, project: str | None = None) -> dict:
    """Bounding box [x,y,w,h] of the main subject of an image layer, in document px."""
    s = _store(project)
    return await _off(s.select_subject_box, layer)


@_strumento()
async def inpaint(box: list | None = None, prompt: str | None = None, ellipse: bool = False,
                  mask_path: str | None = None, feather: float = 6, backend: str = "auto",
                  seed: int | None = None, points: list | None = None, size: float = 30,
                  project: str | None = None) -> dict:
    """Remove an object / fill an area: box=[x,y,w,h] document px, mask_path, or points=[[x,y],...] +
    size (a brush stroke: spot healing). No prompt = remove (classic fill). With prompt = generative
    fill (ComfyUI or an online service: backend=openai, gemini, stability, ...). The result is a patch
    on a NEW layer with a soft mask: move, fade or delete it like any layer."""
    s = _store(project)
    return await _off(s.inpaint, box, prompt, ellipse, mask_path, feather, backend, seed, None, points, size)


@_strumento()
async def generate_image(prompt: str, width: int | None = None, height: int | None = None,
                         negative: str | None = None, seed: int | None = None, fit: str = "cover",
                         backend: str = "auto", project: str | None = None) -> dict:
    """Text-to-image with the local ComfyUI or an online service (backend=openai, gemini, stability,
    openrouter, replicate), placed on a new layer (fit cover/contain)."""
    s = _store(project)
    return await _off(s.generate_image, prompt, width, height, negative, seed, fit, None, backend)


@_strumento()
async def edit_image(prompt: str, layer: str | None = None, box: list | None = None, strength: float = 0.6,
                     backend: str = "auto", seed: int | None = None, project: str | None = None) -> dict:
    """Image-to-image: redo an image layer (or a box of the picture, or all of it) following the prompt
    ("make it winter", "watercolor style"). strength 0..1: low keeps it, high reinvents it. The result
    is a NEW layer above, same place and mask; the original stays below."""
    s = _store(project)
    return await _off(s.edit_image, prompt, layer, box, strength, backend, seed)


@_strumento()
async def describe_image(layer: str | None = None, box: list | None = None, question: str | None = None,
                         backend: str = "auto", project: str | None = None) -> dict:
    """Image-to-text with a vision model (online with a key, or Ollama): describes the picture, a layer
    or a box, or answers question about it. Does not change the document."""
    s = _store(project)
    return await _off(s.describe_image, layer, box, question, backend)


@_strumento()
async def upscale(layer: str, factor: float = 2.0, backend: str = "auto", project: str | None = None) -> dict:
    """More real pixels for an image layer (ComfyUI model, or backend=stability online, else
    Lanczos+sharpen). Its size on the canvas does not change."""
    s = _store(project)
    return await _off(s.upscale, layer, factor, backend)


@_strumento()
async def auto_enhance(layer: str | None = None, strength: float = 1.0, project: str | None = None) -> dict:
    """Measure and correct levels, white balance, vibrance, contrast. Adds normal, editable filters
    (an adjustment layer on top, or effects on layer=)."""
    s = _store(project)
    return await _off(s.auto_enhance, layer, strength)


@_strumento()
async def match_color(layer: str, reference: str, amount: float = 1.0, project: str | None = None) -> dict:
    """Make a layer's colors and tones match another layer (e.g. a pasted subject vs the background)."""
    s = _store(project)
    return await _off(s.match_color, layer, reference, amount)


@_strumento()
def comfy_settings(url: str | None = None, checkpoint: str | None = None, upscaler: str | None = None,
                   steps: int | None = None, cfg: float | None = None) -> dict:
    """Point pedit at a ComfyUI and choose its checkpoint/upscaler. Without args: current settings."""
    return comfy.set_config(url=url, checkpoint=checkpoint, upscaler=upscaler, steps=steps, cfg=cfg)


# --------------------------------------------------------------------------
# esportazione, batch, interfaccia
# --------------------------------------------------------------------------


@_strumento()
async def export_image(path: str, quality: int = 92, scale: float = 1.0, transparent: bool = False,
                       box: list | None = None, project: str | None = None) -> dict:
    """Write the image file (.png .jpg .webp .avif .tif). ONLY when the user asks for it.
    transparent=true keeps alpha (png/webp). box exports a region."""
    s = _store(project)
    api = sys.modules.get("pedit.api")
    if api is not None and api.S.store is s and api.MCP_ATTIVO:
        ok = await anyio.to_thread.run_sync(api.conferma_export, path)
        if not ok:
            return {"exported": False, "reason": "the user did not confirm the export"}
    return await _off(s.export_image, path, quality, scale, transparent, box)


@_strumento()
async def batch(ops: list[dict], look: bool = False, project: str | None = None) -> list | dict:
    """Many edits in ONE call and ONE undo step, all or nothing:
    [{"tool": "set_transform", "args": {...}}, ...]. look=true returns the preview too."""
    s = _store(project)
    risultati = []
    with s.batch():
        for i, op in enumerate(ops):
            nome = op.get("tool")
            if nome in _FUORI_BATCH or nome not in STRUMENTI:
                raise EditError(f"step {i}: {nome!r} cannot be used in batch")
            fn = STRUMENTI[nome]
            args = dict(op.get("args") or {})
            args.pop("project", None)
            try:
                if inspect.iscoroutinefunction(fn):
                    res = await fn(**args)
                else:
                    res = fn(**args)
            except TypeError as exc:
                raise EditError(f"step {i} ({nome}): {exc}") from exc
            except _PREVISTI as exc:
                raise EditError(f"step {i} ({nome}): {_messaggio(exc)}") from exc
            risultati.append(res)
    out = {"done": len(risultati), "results": risultati}
    if look:
        img = await preview(project=project)
        return [img[0], _json(out)]
    return out


@_strumento()
def open_ui(port: int = 8770, project: str | None = None) -> dict:
    """Open the web editor on THIS document (same in-memory document, live updates)."""
    from . import api

    s = _store(project)
    url = api.serve_background(s, port=port)
    return {"url": url}


@_strumento()
def close_ui() -> dict:
    """Stop the web editor started with open_ui."""
    from . import api

    return {"stopped": api.stop_background()}


@_strumento()
def report_issue(kind: str, title: str, details: str = "", tool: str = "") -> dict:
    """Log what is broken/clumsy/missing (kind: bug, scomodo, sbagliato, manca, lento), then go on."""
    return segnalazioni.aggiungi(kind, title, details, tool)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()


__all__ = ["mcp", "main", "STRUMENTI", "PRESETS"]
