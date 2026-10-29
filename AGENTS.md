# Istruzioni per un agente su questa repo

Questo file e' il contratto per chi arriva qui con un agente di codice (Claude Code, Codex,
Cursor, Copilot…). Chi legge deve poter partire da una repo appena clonata e arrivare a un
editor funzionante senza fare domande. `CLAUDE.md` importa questo file.

pedit e' il gemello per le immagini di [vedit](https://github.com/metiutek/editorvideo-ai): stessa
filosofia, stessa struttura. Chi conosce uno si orienta nell'altro.

---

## 0. Se devi solo *usarlo*

```bash
claude mcp add pedit -- uvx pedit-mcp
```

L'interfaccia web viaggia compilata dentro il pacchetto. Il resto di questo file serve a chi la
repo la deve **modificare**.

## 1. Installazione: un comando

```bash
python scripts/setup.py            # --venv, --ai, --no-frontend, --no-test, --rebuild, --reinstall, --json
```

Dipendenze Python (`pip install -e ".[dev,chat]"`), UI compilata (`npm ci && npm run build`),
server MCP in `.mcp.json`, `pedit doctor`, test. **Con `--json` l'unico output e' un JSON** con
`result` (`ok`/`warn`/`fail`) e i passi con `status`, `detail`, `hint`: leggi `hint` prima di
inventarti una diagnosi.

## 2. Verificare che funzioni davvero

```bash
pedit doctor                        # cosa c'e': rembg, OpenCV, ComfyUI, font, UI compilata
pytest                              # ~180 test, ~25 s, niente rete ne' modelli
cd frontend && npm run build && npm test
```

ComfyUI nei test e' sempre "spento" (`conftest.py` punta `PEDIT_COMFY_URL` su una porta chiusa)
e le chiamate generative si provano con `comfy.run` sostituito. I test con rembg vero hanno il
marker `ai` e si saltano se l'extra non c'e'.

## 3. Come e' fatto

Il documento e' **JSON puro** (`model.py`). Il render (`render.py`) e' una funzione pura del
documento piu' i file degli asset: anteprima della UI, `preview` dell'agente ed export escono
dallo stesso codice.

```
backend/pedit/
  model.py       documento: livelli (image, text, shape, fill, adjustment, paint, group), maschere, effetti, asset
  effects.py     registro filtri/stili: parametri, validazione, funzioni numpy
  render.py      livello -> tela del documento: filtri -> maschera -> stili -> opacita' -> fusione
  store.py       tutte le modifiche + undo/redo + salvataggio atomico
  presets.py     look gia' tarati (catene di filtri)
  ai.py          scontorno, riempimento, generazione, image-to-image, upscale, con ripieghi
  cloud.py       modelli in rete con la chiave dell'utente (OpenAI, Gemini, Stability, OpenRouter,
                 Replicate, Anthropic, Ollama, compatibili OpenAI); tutto l'HTTP passa da _http
  comfy.py       client ComfyUI (workflow in formato API)
  analyze.py     misure (stats), correzione automatica (suggest_enhance), revisione (review)
  mcp_server.py  strumenti MCP          api.py  REST/WebSocket + /mcp per la UI
  chat.py        assistente: i suoi strumenti SONO quelli MCP (stessi schemi)
  llm.py         motori: Claude Code, Anthropic, compatibili OpenAI
  cli.py         riga di comando
frontend/src/    React + Vite: Canvas (SVG sopra il render del server), Layers, Properties, Chat
```

**Regola che tiene in piedi tutto: ogni modifica al documento passa da `Store`.** UI, assistente,
agente MCP e CLI chiamano gli stessi metodi. Mai scrivere nel documento da `api.py`,
`mcp_server.py` o `cli.py`.

Scelte non ovvie:

- **Non distruttivo per costruzione.** Lo scontorno IA e' una `Mask(kind="image")` su un asset
  generato; `inpaint` aggiunge un livello toppa con la sua maschera sfumata; `upscale` crea un
  asset nuovo e divide `scale` cosi' la dimensione sulla tela non cambia; `crop_canvas` sposta i
  livelli invece di tagliarli. Gli asset generati stanno in `<documento>_assets/` (o in
  `~/.pedit/scratch/` finche' il documento non e' salvato) e non vengono mai sovrascritti.
- **Maschere in due spazi.** `rect`/`ellipse` hanno il box in pixel del *documento*; `image` e'
  allineata ai pixel del *contenuto* e segue spostamento, scala, rotazione (come una maschera
  collegata di Photoshop).
- **Filtri prima, stili dopo la maschera** (`effects.REGISTRY[...]["kind"]`): un'ombra segue la
  sagoma gia' mascherata.
- **Parametri in pixel (`px=True`) scalano** con l'anteprima e con `resize_image`.
- **Fusioni W3C** con alpha non premoltiplicato (`render.blend`): i test controllano i valori.
- **Gruppi annidati**: `Layer.children`. Si cerca con `project.walk()` / `layer()` / `container()`,
  mai scorrendo solo `project.layers`. `pass_through` (predefinito dei gruppi) compone i figli su cio'
  che sta sotto; gli altri modi isolano il gruppo. `index_of` e' la posizione nella *sua* lista.
- **Maschere di ritaglio** (`clip`): la base e i clip sopra sono una serie (`render._componi`).
- **Pennellate ovunque vettoriali**: maschere dipinte (`Mask.strokes`, in pixel del documento) e timbro
  clone (`Stroke.clone`, copia dal composito sotto il livello: per questo `layer_canvas` riceve `sotto`).
- **IA in rete solo se scelta**: `backend="auto"` usa un servizio a pagamento solo se l'utente l'ha
  reso predefinito (`cloud.scegli`). Le chiavi stanno in `~/.pedit/ai_api.json`, mai nel documento,
  e non passano dagli strumenti MCP. Nei test `cloud._http` e' sempre sostituita.
- **Il rumore ha un seme**: il render deve restare una funzione pura.
- **Anteprime in cache per impronta** (`api.Session.revision`, sha1 del documento): un undo
  ritrova le anteprime gia' fatte.

Quando modifichi:

- **nuovo filtro/stile** → `effects.py`. Il test parametrico lo renderizza; un filtro di colore
  vuole anche un test di *direzione* in `tests/test_render.py` (guardare i pixel, non solo che non
  dia errore).
- **nuovo strumento MCP** → metodo in `store.py`, wrapper in `mcp_server.py` (docstring utile:
  diventa la descrizione), operazione in `api.OPS` se serve alla UI, test in `tests/test_mcp.py`.
  La chat lo prende da sola.
- **cambio alla UI** → `cd frontend && npm run build`, altrimenti il server serve il `dist` vecchio.

## 4. Guidare l'editor da agente (MCP)

```
project_create / project_open -> add_image / add_text / add_shape / add_fill
-> analyze_image -> auto_enhance / add_adjustment / apply_look
-> set_mask / remove_background / inpaint -> preview -> review_image -> export_image
```

- `preview` restituisce **l'immagine vera**: guardala. `grid=true` disegna la griglia al 10% con le
  coordinate in pixel del documento; `box=[x,y,w,h]` ingrandisce un dettaglio.
- `batch` = lista di `{"tool", "args"}` in un blocco atomico con un undo (`batch(look=true)`
  restituisce anche l'anteprima). Fuori da batch: anteprime, export, interfaccia, undo.
- Le risposte sono JSON compatto (`_compatto`): ogni risposta resta nel contesto dell'agente.
  Uno strumento nuovo non deve fare l'eco dei suoi argomenti.
- Lavoro lungo (IA, render grandi) passa da `_off`, che manda i battiti di progresso: Claude Code
  interrompe una chiamata muta dopo 300 s.
- `pedit ui` espone lo stesso server su `/mcp/`: chi si collega li' lavora sul documento aperto
  nel browser (`mcp_server._ospite`). `open_ui` fa il contrario: apre la UI nel processo del server
  stdio, sullo stesso `Store`.
- **L'export non e' una decisione dell'agente**: con l'editor aperto `export_image` chiede
  conferma nel browser (`api.conferma_export`) e senza un si' non scrive.
- `report_issue` annota cio' che non va in `SEGNALAZIONI.md` (radice della repo da un clone,
  altrimenti `~/.pedit/`). Chi modifica la repo parte da li'.

Le regole di ritocco stanno in `mcp_server.ISTRUZIONI` e arrivano a qualunque client;
`tests/test_mcp.py` controlla che restino.

## 5. Convenzioni della repo

- 代码注释和文档字符串使用**简体中文**。界面文案、错误提示、效果、风格和工具说明
  也使用**简体中文**。接口参数、文档字段、枚举值和模型 ID 保持原有标识，中文名称仅用于展示。
  `README.md` 保留英文，`README.it.md` 保留意大利文；修改共同说明时同步两份文档。
- Niente dipendenze nuove senza motivo. Il core sta su numpy, Pillow, FastAPI e mcp. Gli extra
  veri: `ai` (rembg, onnxruntime, OpenCV) e `chat` (anthropic). `tests/test_dipendenze.py` pretende
  che ogni import del backend sia dichiarato in `pyproject.toml`.
- Ogni comportamento nuovo arriva con un test. Prima di dire che funziona: `pytest`, e
  `cd frontend && npm run build && npm test` se hai toccato la UI.
