<p align="center"><img src="frontend/public/logo.svg" alt="pedit logo" width="96"/></p>

<h1 align="center">pedit — tell an AI to edit your image, and watch it happen</h1>

<p align="center">
  <strong>A normal layered image editor — layers, masks, adjustments, brushes, text — except an AI can use it for you.</strong><br/>
  You say what you want in plain words. It works on the layers while you watch, and you can grab the mouse and fix anything at any point.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/AI%20tools-55-blueviolet?style=flat-square" alt="55 AI tools"/>
  <img src="https://img.shields.io/badge/runs-100%25%20local-brightgreen?style=flat-square" alt="Runs locally"/>
  <img src="https://img.shields.io/badge/license-MIT-blue?style=flat-square" alt="License"/>
</p>

> 🇮🇹 Italiano: [README.it.md](README.it.md) · Sister project for video: [editorvideo-ai (vedit)](https://github.com/metiutek/editorvideo-ai)

## Install — 3 steps

**1. Install uv** (it installs Python apps for you), then close and reopen the terminal.

| Windows (PowerShell) | macOS | Linux |
|---|---|---|
| `winget install astral-sh.uv` | `brew install uv` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

**2. Install pedit**

```bash
uv tool install "pedit-mcp[ai]"     # [ai] adds local background removal and object removal
```

**3. Open the editor**

```bash
pedit ui
```

The editor opens at <http://127.0.0.1:8770>. **Optional — let Claude Code edit for you:**
`claude mcp add pedit -- pedit-mcp`. Something wrong? `pedit doctor` says what is missing.

---

## What it is

pedit is an image editor of the ordinary kind: a canvas, a stack of layers, masks, blend
modes, adjustment layers, filters, layer styles, a brush, text and shapes.

The difference is that **everything you can do with the mouse, an AI can also do on command** —
the buttons, the built-in assistant and any MCP agent call the exact same operations. Nothing is
AI-only, nothing is mouse-only. So instead of twenty minutes with the pen tool you type:

> *"Remove the background, put her on a soft blue gradient, warm it up a little, and add the
> title at the bottom with a subtle shadow."*

and the layers appear in the panel, one by one. Wrong shadow? Click the layer and drag the slider.

**Nothing is destructive.** A filter is an entry in the layer's effects, a look is an adjustment
layer, the AI cut-out is a *mask*, an object removal is a *patch on a new layer*. Your photo is
never rewritten, and every step undoes with Ctrl+Z.

## What makes it work

| | |
|---|---|
| **An agent can actually edit** | 55 MCP tools that are the *same* operations as the buttons: layers, transforms, masks, 33 filters and styles, looks, crop, resize, AI tools, export. |
| **The agent sees what it did** | `preview` returns the real rendered image (with an optional coordinate grid, or zoomed on a box). `analyze_image` measures exposure, clipping, colour cast and sharpness, so corrections are measured, not guessed. |
| **It checks its own work** | `review_image` flags text cut by the edge or unreadable on its background, layers off the canvas, blown highlights, upscaled images — each with the tool that fixes it. |
| **AI where it helps, locally** | Background removal (rembg/ONNX), object removal (OpenCV, or a numpy fallback), and — if you run [ComfyUI](https://github.com/comfyanonymous/ComfyUI) — generative fill, text-to-image and model upscaling. |
| **Same document, same pixels** | The document is a JSON file and the render is a pure function of it: the preview, what the agent sees and the exported file come out of the same code. |

## Driving it from an agent (MCP)

The running editor **is** an MCP server at `http://127.0.0.1:8770/mcp/`. An agent connected
there works on the document open in your browser, live:

```bash
claude mcp add --transport http pedit-live http://127.0.0.1:8770/mcp/
```

Or the classic stdio server, for agents that start without a UI (`open_ui` opens the editor on
the agent's document later):

```bash
claude mcp add pedit -- uvx pedit-mcp
```

Same JSON for Cursor, Codex, VS Code: `{ "mcpServers": { "pedit": { "command": "pedit-mcp" } } }`.

Typical flow:

```
project_create / project_open(photo) → add_image / add_text / add_shape / add_fill
  → analyze_image → auto_enhance or add_adjustment → apply_look
  → remove_background / set_mask / inpaint → preview (look!) → review_image → export_image
```

`batch` applies a list of edits in one atomic call with one undo step, and `batch(look=true)`
returns the preview in the same round trip. The editing rules (look before claiming done,
measure before correcting, base before style, stay non-destructive, never export unasked) ship
inside the server instructions, so every client gets them.

## The web interface

```
┌──────────────────────────────────────────────────────────────────────┐
│ New · Open · Save · ↶↷ · Import · Generate · Auto enhance   Export   │
│ tool options (brush size, text, selection actions…)                  │
├────┬──────────────────────────────────────────┬──────────────────────┤
│ ✥  │                                          │ Layers | Looks | AI  │
│ ⬚  │              canvas                      │  layer stack         │
│ ◯  │      (server-rendered, exact)            │  properties, mask,   │
│ ✂  │                                          │  effects, AI buttons │
│ ✎  │                                          │                      │
└────┴──────────────────────────────────────────┴──────────────────────┘
```

- **Tools:** move/resize (V), rectangle (M) and ellipse (L) selection, crop (C), brush (B),
  eraser (E), text (T), shapes (U), pan (H). With a selection: *mask layer*, *remove object*,
  *generative fill*, *crop to selection*.
- **Layers:** visibility, lock, rename (double click), drag to reorder, duplicate, merge down,
  opacity and 14 blend modes. Image, text, shape, fill/gradient, paint and adjustment layers.
- **Looks:** cinematic, black & white, warm, cool, faded film, vivid, matte, vintage, dramatic,
  clean portrait — each one an editable chain of filters.
- **Assistant:** Claude Code (your subscription, no key), Anthropic with a key, or any
  OpenAI-compatible service (OpenAI, Gemini, OpenRouter, Ollama, LM Studio…). Point at an area
  (`+ area`) or a layer (`+ layer`) and refer to it in words. Exports always ask you first.
- Drop images on the window to import them. Ctrl+Z/Y, Ctrl+S, Ctrl+J duplicate, Del delete,
  0 fit, 1 = 100%, Ctrl+wheel zoom.

## Command line

```bash
pedit new poster.json --preset instagram_post
pedit add poster.json photo.jpg --fit cover
pedit text poster.json "SUMMER SALE" --size 140 --bold --align center-bottom
pedit enhance poster.json
pedit look poster.json warm
pedit cutout poster.json <layer-id>          # needs the ai extra
pedit export poster.json poster.jpg --quality 90
pedit review poster.json
pedit effects -v | pedit looks | pedit doctor | pedit ui
```

## Optional extras

| Extra | What it adds |
|---|---|
| `ai` | `rembg` + `onnxruntime` (background removal, models downloaded on first use) and OpenCV (better object removal) |
| `chat` | `anthropic` — the assistant with an Anthropic API key (other engines need nothing) |
| ComfyUI | not a pip package: run it locally (default `127.0.0.1:8188`) and `generate_image`, `inpaint(prompt=…)` and model `upscale` light up. `comfy_settings` picks the URL, checkpoint and upscaler. |

## How it works

```
backend/pedit/
  model.py      document (dataclass ↔ JSON): layers, masks, effects, assets
  effects.py    filter and layer-style registry: params, validation, numpy kernels
  render.py     document → pixels (W3C blend modes, masks, styles), export
  store.py      every edit + undo/redo + atomic save — the only way to change a document
  ai.py         background removal, fill, generation, upscale (with fallbacks)
  comfy.py      minimal ComfyUI client (txt2img, inpaint, upscale workflows)
  analyze.py    measurements and document review
  mcp_server.py MCP tools        api.py  REST/WebSocket for the UI
  chat.py/llm.py the in-app assistant (its tools are the MCP tools)
frontend/       React + Vite UI
tests/          pixel-level tests of every effect, blend mode, mask and operation
```

**One source of truth:** the UI, the assistant and the MCP agent all call methods of `Store`.
Add an operation there first, then expose it.

## Tests

```bash
pytest                         # ~120 tests, ~20 s, no network, no models
cd frontend && npm run build && npm test
```

## Development

```bash
git clone https://github.com/metiutek/editorimage-ai && cd editorimage-ai
python scripts/setup.py --venv        # deps, UI build, MCP registration, doctor, tests
```

Code and comments are in Italian (project convention); everything a user reads is in English.
See [AGENTS.md](AGENTS.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
