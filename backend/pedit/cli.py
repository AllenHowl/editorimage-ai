"""命令行入口：对同一文档执行与界面相同的操作。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, ai, presets, render
from . import effects as fx
from .store import PRESETS, EditError, Store


def _stampa(x) -> None:
    print(json.dumps(x, indent=2, ensure_ascii=False, default=str))


def cmd_new(a) -> None:
    s = Store.create(name=a.name or Path(a.project).stem, preset=a.preset, width=a.width, height=a.height,
                     background=a.background, path=a.project, from_image=a.from_image)
    _stampa({"path": s.path, "width": s.project.width, "height": s.project.height})


def cmd_info(a) -> None:
    _stampa(Store.open(a.project).summary("full" if a.full else "normal"))


def cmd_add(a) -> None:
    s = Store.open(a.project)
    for f in a.images:
        _stampa(s.add_image(path=f, fit=a.fit))


def cmd_text(a) -> None:
    s = Store.open(a.project)
    out = s.add_text(a.text, size=a.size, color=a.color, font=a.font, bold=a.bold)
    if a.align:
        h, _, v = a.align.partition("-")
        out = s.align_layer(out["id"], h or "center", v or "middle", a.margin)
    _stampa(out)


def cmd_effect(a) -> None:
    s = Store.open(a.project)
    params = json.loads(a.params) if a.params else {}
    if a.layer:
        _stampa(s.add_effect(a.type, a.layer, params))
    else:
        _stampa(s.add_adjustment(a.type, params))


def cmd_look(a) -> None:
    _stampa(Store.open(a.project).apply_look(a.name, a.layer))


def cmd_enhance(a) -> None:
    _stampa(Store.open(a.project).auto_enhance(a.layer, a.strength))


def cmd_cutout(a) -> None:
    _stampa(Store.open(a.project).remove_background(a.layer, a.keep, a.model))


def cmd_export(a) -> None:
    s = Store.open(a.project)
    _stampa(s.export_image(a.output, quality=a.quality, scale=a.scale, transparent=a.transparent))


def cmd_review(a) -> None:
    from . import analyze

    s = Store.open(a.project)
    _stampa(analyze.review(s.project, s.base_dir))


def cmd_effects(a) -> None:
    for e in fx.describe():
        print(f"{e['name']:22} {e['kind']:7} {e['desc']}")
        if a.verbose:
            for k, p in e["params"].items():
                rng = f"[{p.get('min')}, {p.get('max')}]" if "min" in p else ""
                print(f"    {k:14} {p['type']:7} default={p['default']} {rng}  {p['desc']}")


def cmd_looks(a) -> None:
    for x in presets.describe():
        print(f"{x['name']:16} {x['desc']}")


def cmd_doctor(a) -> None:
    import importlib.util

    righe = [("python", sys.version.split()[0], True)]
    for mod in ("numpy", "PIL", "fastapi", "mcp"):
        righe.append((mod, "ok" if importlib.util.find_spec(mod) else "MISSING", bool(importlib.util.find_spec(mod))))
    st = ai.status()
    righe.append(("background removal", "ok" if st["remove_background"]["ok"] else st["remove_background"]["hint"],
                  st["remove_background"]["ok"]))
    righe.append(("inpaint", st["inpaint"]["backend"], True))
    c = st["comfyui"]
    righe.append(("ComfyUI", f"{c['url']} ({len(c.get('checkpoints', []))} checkpoints)" if c["ok"] else
                  f"not reachable at {c['url']} - generative tools off", c["ok"]))
    font = render.font_path("sans")
    righe.append(("fonts", str(font) if font else "no system font found: text uses Pillow's default", bool(font)))
    from .api import FRONTEND

    ui = (FRONTEND / "index.html").is_file()
    righe.append(("web UI", str(FRONTEND) if ui else "not built: cd frontend && npm ci && npm run build", ui))
    for nome, val, ok in righe:
        print(f"{'ok ' if ok else '-- '} {nome:20} {val}")


def cmd_ui(a) -> None:
    from . import api

    s = None
    if a.project:
        p = Path(a.project)
        s = Store.open(str(p)) if p.exists() else Store.create(name=p.stem, path=str(p))
    api.serve(s, host=a.host, port=a.port, browser=not a.no_browser)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="pedit", description="Layered image editor an AI agent can drive.")
    ap.add_argument("--version", action="version", version=f"pedit {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="create a document")
    p.add_argument("project")
    p.add_argument("--preset", choices=list(PRESETS))
    p.add_argument("--width", type=int)
    p.add_argument("--height", type=int)
    p.add_argument("--background", default="#ffffff")
    p.add_argument("--name")
    p.add_argument("--from-image", dest="from_image")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("info", help="layers and assets")
    p.add_argument("project")
    p.add_argument("--full", action="store_true")
    p.set_defaults(fn=cmd_info)

    p = sub.add_parser("add", help="add image layers")
    p.add_argument("project")
    p.add_argument("images", nargs="+")
    p.add_argument("--fit", default="contain", choices=["contain", "cover", "width", "height", "none"])
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("text", help="add a text layer")
    p.add_argument("project")
    p.add_argument("text")
    p.add_argument("--size", type=float, default=64)
    p.add_argument("--color", default="#ffffff")
    p.add_argument("--font", default="sans")
    p.add_argument("--bold", action="store_true")
    p.add_argument("--align", help="e.g. center-bottom, left-top")
    p.add_argument("--margin", type=float, default=40)
    p.set_defaults(fn=cmd_text)

    p = sub.add_parser("effect", help="add an effect (on a layer, or as an adjustment layer)")
    p.add_argument("project")
    p.add_argument("type")
    p.add_argument("--layer")
    p.add_argument("--params", help='JSON, e.g. \'{"amount": 0.3}\'')
    p.set_defaults(fn=cmd_effect)

    p = sub.add_parser("look", help="apply a look")
    p.add_argument("project")
    p.add_argument("name", choices=list(presets.LOOKS))
    p.add_argument("--layer")
    p.set_defaults(fn=cmd_look)

    p = sub.add_parser("enhance", help="automatic correction")
    p.add_argument("project")
    p.add_argument("--layer")
    p.add_argument("--strength", type=float, default=1.0)
    p.set_defaults(fn=cmd_enhance)

    p = sub.add_parser("cutout", help="AI background removal as a mask")
    p.add_argument("project")
    p.add_argument("layer")
    p.add_argument("--keep", default="subject", choices=["subject", "background"])
    p.add_argument("--model", default="isnet-general-use")
    p.set_defaults(fn=cmd_cutout)

    p = sub.add_parser("export", help="write the image")
    p.add_argument("project")
    p.add_argument("output")
    p.add_argument("--quality", type=int, default=92)
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--transparent", action="store_true")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("review", help="check the document for problems")
    p.add_argument("project")
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("effects", help="effect catalogue")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_effects)

    p = sub.add_parser("looks", help="look catalogue")
    p.set_defaults(fn=cmd_looks)

    p = sub.add_parser("doctor", help="what is installed and what is missing")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("ui", help="open the web editor")
    p.add_argument("--project")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(fn=cmd_ui)

    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except (EditError, ValueError, KeyError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
