<p align="center"><img src="frontend/public/logo.svg" alt="logo pedit" width="96"/></p>

<h1 align="center">pedit — editor di immagini open source con IA, da guidare a parole (MCP)</h1>

<p align="center">
  <strong>Un editor di foto a livelli gratuito — un'alternativa a Photoshop e GIMP — che un agente IA puo' usare al posto tuo.</strong><br/>
  Livelli, gruppi, maschere, maschere di ritaglio, 27 metodi di fusione, livelli di regolazione, timbro clone e pennello correttivo,
  piu' riempimento ed espansione generativi, image-to-image, generazione da testo, scontorno e upscale —
  in locale o con la tua API key di OpenAI, Gemini, Stability AI, Replicate o OpenRouter.
</p>

<p align="center"><img src="docs/promo.gif" alt="pedit promo: un agente IA modifica un'immagine via MCP" width="720"/></p>

Dici cosa vuoi a parole — *"togli lo sfondo, allunga il cielo, rendila piu' calda"* — e lavora sui
livelli veri mentre guardi; puoi riprendere il mouse e correggere qualunque cosa in qualunque momento.
Funziona con **Claude Code, Cursor, Codex, VS Code** e qualunque client [Model Context Protocol](https://modelcontextprotocol.io).

> 🇬🇧 English: [README.md](README.md) · Progetto gemello per il video: [editorvideo-ai (vedit)](https://github.com/metiutek/editorvideo-ai)

## Installazione — 3 passi

**1. Installa uv**, poi chiudi e riapri il terminale.

| Windows (PowerShell) | macOS | Linux |
|---|---|---|
| `winget install astral-sh.uv` | `brew install uv` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

**2. Installa pedit**

```bash
uv tool install "pedit-mcp[ai]"
```

`[ai]` aggiunge scontorno e rimozione oggetti in locale.

**3. Apri l'editor**

```bash
pedit ui
```

Si apre su <http://127.0.0.1:8770>. **Facoltativo — fai ritoccare a Claude Code:**
`claude mcp add pedit -- pedit-mcp`. Qualcosa non va? `pedit doctor` dice cosa manca.

---

## Cos'e'

Un editor di immagini normale: una tela, una pila di livelli, maschere, metodi di fusione,
livelli di regolazione, filtri, stili di livello, pennello, testo e forme.

La differenza: **tutto cio' che fai col mouse lo puo' fare anche un'IA a comando** — i
pulsanti, l'assistente interno e qualunque agente MCP chiamano le stesse identiche operazioni.
Niente e' solo-IA, niente e' solo-mouse. Invece di venti minuti di tracciati scrivi:

> *"Togli lo sfondo, mettila su un gradiente azzurro morbido, scaldala un po' e aggiungi il
> titolo in basso con un'ombra leggera."*

e i livelli compaiono nel pannello uno alla volta. Ombra sbagliata? Clic sul livello, sposti lo slider.

**Niente e' distruttivo.** Un filtro e' una voce negli effetti del livello, un look e' un
livello di regolazione, lo scontorno IA e' una *maschera*, la rimozione di un oggetto e' una
*toppa su un livello nuovo*. La foto non viene mai riscritta, e ogni passo si annulla con Ctrl+Z.

## Cosa lo fa funzionare

| | |
|---|---|
| **Un agente modifica davvero** | 62 strumenti MCP che sono le *stesse* operazioni dei pulsanti: livelli e gruppi, maschere di ritaglio, maschere dipinte, trasformazioni, 33 filtri e stili, 27 metodi di fusione, look, timbro clone, ritaglio, ridimensionamento, IA, export. |
| **L'agente vede cosa ha fatto** | `preview` restituisce l'immagine renderizzata vera (con griglia di coordinate, o ingrandita su un riquadro). `analyze_image` misura esposizione, pixel bruciati, dominante, nitidezza. |
| **Controlla il proprio lavoro** | `review_image` segnala testi tagliati o illeggibili sullo sfondo, livelli fuori tela, alte luci bruciate, immagini ingrandite — ognuno con lo strumento che lo sistema. |
| **IA in locale, o in rete con la tua chiave** | Scontorno (rembg/ONNX), rimozione oggetti e pennello correttivo al volo (OpenCV o ripiego numpy) e — con [ComfyUI](https://github.com/comfyanonymous/ComfyUI) o un servizio in rete — generazione da testo, image-to-image, riempimento ed espansione generativi, upscale con modello. Servizi con la tua API key: OpenAI (GPT Image), Google Gemini (Nano Banana), Stability AI, OpenRouter, Replicate (FLUX…), qualunque endpoint compatibile OpenAI; image-to-text con Gemini, OpenAI, Claude o un modello di visione locale di Ollama. |
| **Stesso documento, stessi pixel** | Il documento e' un file JSON e il render ne e' una funzione pura: anteprima, immagine dell'agente e file esportato escono dallo stesso codice. |

## Guidarlo da un agente (MCP)

L'editor aperto **e'** un server MCP su `http://127.0.0.1:8770/mcp/`: un agente collegato li'
lavora sul documento che vedi nel browser, dal vivo.

```bash
claude mcp add --transport http pedit-live http://127.0.0.1:8770/mcp/
claude mcp add pedit -- uvx pedit-mcp
```

Flusso tipico: `project_create / project_open(foto) → add_image / add_text → analyze_image →
auto_enhance / add_adjustment → apply_look → remove_background / set_mask / inpaint → preview →
review_image → export_image`. `batch` applica tante modifiche in una chiamata atomica con un solo
undo. Le regole di ritocco viaggiano nelle istruzioni del server.

## Riga di comando

```bash
pedit new poster.json --preset instagram_post
pedit add poster.json foto.jpg --fit cover
pedit text poster.json "SALDI" --size 140 --bold --align center-bottom
pedit enhance poster.json && pedit look poster.json warm
pedit export poster.json poster.jpg --quality 90
```

## Extra facoltativi

| Extra | Cosa aggiunge |
|---|---|
| `ai` | `rembg` + `onnxruntime` (scontorno, modelli scaricati al primo uso) e OpenCV (rimozione oggetti migliore) |
| `chat` | `anthropic` — l'assistente con una chiave Anthropic (gli altri motori non vogliono niente) |
| ComfyUI | non e' un pacchetto pip: avvialo in locale (`127.0.0.1:8188`) e si accendono `generate_image`, `edit_image`, `inpaint(prompt=…)`, `expand_canvas` e `upscale` con modello |
| Modelli in rete | nemmeno loro: incolla una API key in *AI models* (o `OPENAI_API_KEY`, `GEMINI_API_KEY`, `STABILITY_API_KEY`, `OPENROUTER_API_KEY`, `REPLICATE_API_TOKEN`, `ANTHROPIC_API_KEY`) e passa `backend=<servizio>`, oppure rendine uno il predefinito di `auto` |

## Test e sviluppo

```bash
python scripts/setup.py --venv
pytest
cd frontend && npm run build && npm test
```

Codice e commenti in italiano; cio' che legge l'utente in inglese. Leggi [AGENTS.md](AGENTS.md).

## Licenza

MIT — vedi [LICENSE](LICENSE).

---

Realizzato da [metiu.xyz](https://metiu.xyz/)
