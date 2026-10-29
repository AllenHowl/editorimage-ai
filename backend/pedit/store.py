"""文档编辑操作及持久化。

本模块是唯一的文档修改入口。MCP 服务、界面 API、聊天助手和命令行
调用同一组方法，确保界面与智能体行为一致。每次修改前保存快照以支持
撤销和重做；文档已有路径时，修改后自动保存。

AI 操作也通过此入口，保持非破坏式：抠图生成蒙版，移除物体生成
新图层上的补丁，放大生成新素材，始终保留原始图片。
"""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import ai
from . import analyze
from . import effects as fx
from . import presets
from . import render
from .model import (
    BLEND_MODES,
    FILL_KINDS,
    GROUP_BLEND_MODES,
    IMAGE_EXTS,
    MASK_KINDS,
    SHAPE_KINDS,
    Asset,
    Effect,
    Fill,
    Layer,
    Mask,
    Project,
    Shape,
    Stroke,
    TextStyle,
    new_id,
)

MAX_HISTORY = 100

PRESETS = {
    "1080p": (1920, 1080),
    "4k": (3840, 2160),
    "square": (1080, 1080),
    "instagram_post": (1080, 1350),
    "story": (1080, 1920),
    "youtube_thumbnail": (1280, 720),
    "a4_300dpi": (2480, 3508),
    "a4_150dpi": (1240, 1754),
    "web_banner": (1500, 500),
    "og_image": (1200, 630),
}

ALIGN_H = ("left", "center", "right")
ALIGN_V = ("top", "middle", "bottom")


class EditError(ValueError):
    """带有用户可读提示的编辑错误。"""


def _home() -> Path:
    base = Path(os.environ.get("PEDIT_HOME") or (Path.home() / ".pedit"))
    base.mkdir(parents=True, exist_ok=True)
    return base


class Store:
    def __init__(self, project: Project, path: str | None = None, autosave: bool = True):
        self.project = project
        self.path = str(Path(path).resolve()) if path else None
        self.autosave = autosave
        self._undo: list[dict] = []
        self._redo: list[dict] = []
        self._batch = 0
        self.version = 0
        # 每次修改完成后调用，即使由智能体操作，界面也能同步更新。
        # 共用 Store，保持单一写入入口。
        self.on_change = None
        # 尚未保存文档时使用的素材目录。
        self._scratch: Path | None = None

    # ---- 生命周期 ------------------------------------------------------
    @classmethod
    def create(cls, name: str = "未命名", preset: str | None = None, width: int | None = None,
               height: int | None = None, background: str = "#ffffff", path: str | None = None,
               dpi: int = 72, from_image: str | None = None) -> "Store":
        if from_image:
            img = cls._probe(from_image)
            w, h = img
        elif preset:
            if preset not in PRESETS:
                raise EditError(f"未知预设 {preset!r}；可选：{', '.join(PRESETS)}")
            w, h = PRESETS[preset]
        else:
            w, h = 1920, 1080
        w, h = int(width or w), int(height or h)
        if not (1 <= w <= 20000 and 1 <= h <= 20000):
            raise EditError("宽度和高度必须在 1 到 20000 像素之间")
        fx.parse_color(background)
        s = cls(Project(name=name, width=w, height=h, background=background, dpi=dpi), path)
        if from_image:
            s._autosave_off(lambda: s.add_image(path=from_image, x=0, y=0, fit="none", name=Path(from_image).stem))
            s._undo.clear()
        if path:
            s.save()
        return s

    def _autosave_off(self, fn):
        old, self.autosave = self.autosave, False
        try:
            return fn()
        finally:
            self.autosave = old

    @classmethod
    def open(cls, path: str) -> "Store":
        f = Path(path)
        if not f.exists():
            raise EditError(f"未找到文档：{path}")
        if f.suffix.lower() in IMAGE_EXTS:
            # 打开照片时创建与照片同尺寸、尚未保存的新文档。
            return cls.create(name=f.stem, from_image=str(f))
        data = json.loads(f.read_text(encoding="utf-8"))
        return cls(Project.from_obj(data), str(f.resolve()))

    def save(self, path: str | None = None) -> str:
        """使用 ``os.replace`` 原子保存，避免留下写入不完整的文件。"""
        target = Path(path or self.path or "")
        if not str(target):
            raise EditError("缺少保存路径，请传入 path=")
        if target.suffix.lower() != ".json":
            raise EditError("pedit 文档必须为 .json 文件；导出图片请使用 export_image")
        target.parent.mkdir(parents=True, exist_ok=True)
        vecchia_base = self.base_dir
        self.path = str(target.resolve())
        self._riloca_asset(vecchia_base)
        blob = json.dumps(self.project.to_dict(), indent=2, ensure_ascii=False)
        last: OSError | None = None
        for attempt in range(10):
            tmp = target.with_name(f"~{uuid.uuid4().hex[:8]}_{target.name}")
            try:
                tmp.write_text(blob, encoding="utf-8")
                os.replace(tmp, target)
                break
            except OSError as exc:
                tmp.unlink(missing_ok=True)
                last = exc
                time.sleep(0.02 * (attempt + 1))
        else:
            raise EditError(f"保存到 {target} 失败：{last}")
        return self.path

    def _riloca_asset(self, vecchia_base: str | None) -> None:
        """文档移动到其他目录后，保持相对素材路径有效。"""
        for a in self.project.assets:
            p = render.resolve(a.path, vecchia_base)
            a.path = self._percorso_asset(p)

    def _percorso_asset(self, p: Path) -> str:
        p = p.resolve()
        if self.base_dir:
            try:
                return p.relative_to(self.base_dir).as_posix()
            except ValueError:
                pass
        return str(p)

    @property
    def base_dir(self) -> str | None:
        return str(Path(self.path).parent) if self.path else None

    def assets_dir(self) -> Path:
        """获取保存 AI 蒙版、填充补丁和栅格化图层的目录。"""
        if self.path:
            d = Path(self.path).with_name(Path(self.path).stem + "_assets")
        else:
            if self._scratch is None:
                self._scratch = _home() / "scratch" / uuid.uuid4().hex[:12]
            d = self._scratch
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---- 撤销和重做 ----------------------------------------------------
    def _touch(self) -> None:
        if self._batch:
            return
        self._undo.append(self.project.to_dict())
        if len(self._undo) > MAX_HISTORY:
            self._undo.pop(0)
        self._redo.clear()

    def _done(self) -> None:
        if self._batch:
            return
        self.version += 1
        if self.autosave and self.path:
            self.save()
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001 - 监听器出错不应撤销已完成的修改。
                pass

    @contextmanager
    def batch(self):
        """将多项操作合并为一次撤销和保存，整体成功或整体回滚。"""
        if self._batch:
            self._batch += 1
            try:
                yield self
            finally:
                self._batch -= 1
            return
        self._undo.append(self.project.to_dict())
        if len(self._undo) > MAX_HISTORY:
            self._undo.pop(0)
        self._redo.clear()
        self._batch = 1
        try:
            yield self
        except Exception:
            self.project = Project.from_obj(self._undo.pop())
            self.version += 1
            raise
        finally:
            self._batch = 0
        self._done()

    def undo(self) -> dict:
        if not self._undo:
            raise EditError("没有可撤销的操作")
        self._redo.append(self.project.to_dict())
        self.project = Project.from_obj(self._undo.pop())
        self._done()
        return {"undo": len(self._undo), "redo": len(self._redo)}

    def redo(self) -> dict:
        if not self._redo:
            raise EditError("没有可重做的操作")
        self._undo.append(self.project.to_dict())
        self.project = Project.from_obj(self._redo.pop())
        self._done()
        return {"undo": len(self._undo), "redo": len(self._redo)}

    @contextmanager
    def _edit(self):
        """单次修改：先保存快照，成功后落盘，失败时恢复原状态。"""
        prima = self.project.to_dict() if not self._batch else None
        self._touch()
        try:
            yield
        except Exception:
            if prima is not None:
                self.project = Project.from_obj(prima)
                self._undo.pop()
            raise
        self._done()

    # ---- 读取 ----------------------------------------------------------
    def summary(self, detail: str = "normal") -> dict:
        p = self.project
        out = {"name": p.name, "width": p.width, "height": p.height, "background": p.background,
               "dpi": p.dpi, "path": self.path, "undo": len(self._undo), "redo": len(self._redo)}
        out["layers"] = [self.layer_view(ly, detail) for ly in reversed(p.layers)]
        if detail == "full":
            out["assets"] = [vars(a) | {} for a in p.assets]
        else:
            out["assets"] = [{"id": a.id, "name": a.name, "width": a.width, "height": a.height,
                              "generated": a.generated} for a in p.assets]
        return out

    def layer_view(self, ly: Layer, detail: str = "normal") -> dict:
        """返回图层的紧凑摘要，提供决定下一步所需的信息。"""
        v: dict[str, Any] = {"id": ly.id, "name": ly.name, "type": ly.type}
        for k, default in (("visible", True), ("locked", False), ("opacity", 1.0), ("blend", "normal")):
            if getattr(ly, k) != default or detail == "full":
                v[k] = getattr(ly, k)
        if ly.type in ("image", "text", "shape"):
            try:
                w, h = render.content_size(ly, self.project, self.base_dir)
            except (KeyError, FileNotFoundError, OSError):
                w, h = 0, 0
            v["box"] = [round(ly.x, 1), round(ly.y, 1), round(w, 1), round(h, 1)]
            if ly.scale != 1:
                v["scale"] = round(ly.scale, 4)
            if ly.rotation:
                v["rotation"] = ly.rotation
            if ly.flip_h or ly.flip_v:
                v["flip"] = [ly.flip_h, ly.flip_v]
        if ly.type == "image":
            v["asset"] = ly.asset
            if ly.crop:
                v["crop"] = ly.crop
        if ly.type == "text":
            v["text"] = ly.text
            st = ly.style
            v["style"] = vars(st) if detail == "full" else {"font": st.font, "size": st.size, "color": st.color}
        if ly.type == "shape":
            v["shape"] = vars(ly.shape)
        if ly.type == "fill":
            v["fill"] = vars(ly.fill)
        if ly.type == "paint":
            v["strokes"] = len(ly.strokes)
            cloni = sum(1 for st in ly.strokes if st.clone)
            if cloni:
                v["clone_strokes"] = cloni
        if ly.clip:
            v["clip"] = True
        if ly.type == "group":
            v["children"] = [self.layer_view(c, detail) for c in reversed(ly.children)]
            if ly.collapsed:
                v["collapsed"] = True
        if ly.effects:
            v["effects"] = [{"type": e.type, **({"params": e.params} if detail == "full" else {}),
                             **({} if e.enabled else {"enabled": False})} for e in ly.effects]
        if ly.mask:
            v["mask"] = {k: x for k, x in vars(ly.mask).items()
                         if k != "strokes" and (x not in (None, False, 0.0) or k == "kind") and not (k == "base" and x == 1.0)}
            if ly.mask.strokes:
                v["mask"]["strokes"] = len(ly.mask.strokes)
        return v

    # ---- 辅助方法 ------------------------------------------------------
    def _layer(self, layer: str, edit: bool = True) -> Layer:
        if not layer:
            raise EditError("请通过 layer=<ID 或名称> 指定图层")
        try:
            ly = self.project.layer(layer)
        except KeyError as exc:
            ids = ", ".join(f"{x.id} ({x.name})" for x in self.project.walk()) or "none"
            raise EditError(f"未找到图层：{layer}；现有图层：{ids}") from exc
        if edit and ly.locked:
            raise EditError(f"图层 {ly.name or ly.id} 已锁定，请先调用 set_layer(locked=false)")
        return ly

    def _insert(self, ly: Layer, index: int | None = None, above: str | None = None) -> None:
        if above:
            rif = self._layer(above, edit=False)
            lista = self.project.container(rif.id)
            lista.insert(self.project.index_of(rif.id) + 1, ly)
            return
        if index is None:
            i = len(self.project.layers)
        else:
            i = max(0, min(int(index), len(self.project.layers)))
        self.project.layers.insert(i, ly)

    def _rimpiazza(self, vecchio: Layer, nuovo: Layer) -> None:
        lista = self.project.container(vecchio.id)
        lista[next(i for i, x in enumerate(lista) if x is vecchio)] = nuovo

    def _nome(self, base: str) -> str:
        nomi = {ly.name for ly in self.project.walk()}
        if base not in nomi:
            return base
        n = 2
        while f"{base} {n}" in nomi:
            n += 1
        return f"{base} {n}"

    @staticmethod
    def _probe(path: str) -> tuple[int, int]:
        p = Path(path)
        if not p.is_file():
            raise EditError(f"未找到图片：{path}")
        try:
            with Image.open(p) as im:
                from PIL import ImageOps
                im2 = ImageOps.exif_transpose(im)
                return im2.size
        except OSError as exc:
            raise EditError(f"无法读取图片：{path} ({exc})") from exc

    def _new_asset(self, img: Image.Image, name: str, prefix: str = "gen") -> Asset:
        d = self.assets_dir()
        f = d / f"{prefix}_{uuid.uuid4().hex[:8]}.png"
        img.save(f, "PNG")
        a = Asset(id=new_id("a"), path=self._percorso_asset(f), width=img.width, height=img.height,
                  name=name, generated=True)
        self.project.assets.append(a)
        return a

    def _asset_img(self, asset_id: str) -> Image.Image:
        a = self.project.asset(asset_id)
        return render.load_image(render.resolve(a.path, self.base_dir))

    @staticmethod
    def _check_box(box: Any, what: str = "box") -> list[float]:
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            raise EditError(f"{what} 必须为文档像素区域 [x, y, width, height]")
        x, y, w, h = (float(v) for v in box)
        if w <= 0 or h <= 0:
            raise EditError(f"{what}：宽度和高度必须为正数")
        return [x, y, w, h]

    def _clip_box(self, box: list[float]) -> tuple[int, int, int, int]:
        x, y, w, h = box
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(self.project.width, int(round(x + w))), min(self.project.height, int(round(y + h)))
        if x1 <= x0 or y1 <= y0:
            raise EditError("选区超出画布范围")
        return x0, y0, x1, y1

    # ---- 文档 ----------------------------------------------------------
    def set_document(self, name: str | None = None, background: str | None = None,
                     dpi: int | None = None) -> dict:
        with self._edit():
            if name is not None:
                self.project.name = name
            if background is not None:
                fx.parse_color(background)
                self.project.background = background
            if dpi is not None:
                self.project.dpi = int(dpi)
        return {"name": self.project.name, "background": self.project.background, "dpi": self.project.dpi}

    def import_images(self, paths: list[str]) -> list[dict]:
        """将文件登记为素材，不复制文件，也不创建图层。"""
        if isinstance(paths, str):
            paths = [paths]
        with self._edit():
            nuovi = [self.project.asset(self._import_one(p)) for p in paths]
        return [{"id": a.id, "name": a.name, "width": a.width, "height": a.height} for a in nuovi]

    def remove_asset(self, asset: str) -> dict:
        a = self.project.asset(asset)
        usato = [ly.id for ly in self.project.walk()
                 if ly.asset == a.id or (ly.mask and ly.mask.asset == a.id)]
        if usato:
            raise EditError(f"素材 {asset} 正被以下图层使用：{usato}")
        with self._edit():
            self.project.assets.remove(a)
        return {"removed": asset}

    # ---- 图层：创建 ----------------------------------------------------
    def add_image(self, asset: str | None = None, path: str | None = None, x: float | None = None,
                  y: float | None = None, scale: float | None = None, fit: str = "contain",
                  name: str | None = None, index: int | None = None, above: str | None = None) -> dict:
        """创建图片图层。``fit``：contain 完整放入画布，cover 铺满画布，或 width、none。"""
        if fit not in ("contain", "cover", "width", "height", "none"):
            raise EditError("fit 必须为 contain、cover、width、height 或 none")
        with self._edit():
            if asset is None:
                if not path:
                    raise EditError("请传入 asset=<ID> 或 path=<文件>")
                asset = self._import_one(path)
            a = self.project.asset(asset)
            W, H = self.project.width, self.project.height
            if scale is None:
                if fit == "none":
                    scale = 1.0
                elif fit == "contain":
                    scale = min(1.0, W / a.width, H / a.height)
                elif fit == "cover":
                    scale = max(W / a.width, H / a.height)
                elif fit == "width":
                    scale = W / a.width
                else:
                    scale = H / a.height
            if scale <= 0:
                raise EditError("缩放比例必须为正数")
            w, h = a.width * scale, a.height * scale
            ly = Layer(id=new_id("L"), type="image", name=self._nome(name or a.name or "图片"), asset=a.id,
                       scale=float(scale),
                       x=float((W - w) / 2 if x is None else x), y=float((H - h) / 2 if y is None else y))
            self._insert(ly, index, above)
        return self.layer_view(ly)

    def _import_one(self, path: str) -> str:
        w, h = self._probe(path)
        p = Path(path).resolve()
        for a in self.project.assets:
            if render.resolve(a.path, self.base_dir).resolve() == p:
                return a.id
        a = Asset(id=new_id("a"), path=self._percorso_asset(p), width=w, height=h, name=p.stem)
        self.project.assets.append(a)
        return a.id

    def add_text(self, text: str, x: float | None = None, y: float | None = None, size: float = 64,
                 color: str = "#ffffff", font: str = "sans", bold: bool = False, italic: bool = False,
                 align: str = "left", line_spacing: float = 1.2, stroke_width: float = 0,
                 stroke_color: str = "#000000", max_width: float = 0, name: str | None = None,
                 index: int | None = None, above: str | None = None) -> dict:
        if not text:
            raise EditError("文字不能为空")
        if align not in ("left", "center", "right"):
            raise EditError("align 必须为 left、center 或 right")
        if size <= 0:
            raise EditError("字号必须为正数")
        fx.parse_color(color)
        fx.parse_color(stroke_color)
        with self._edit():
            st = TextStyle(font=font, size=float(size), color=color, bold=bold, italic=italic, align=align,
                           line_spacing=float(line_spacing), stroke_width=float(stroke_width),
                           stroke_color=stroke_color, max_width=float(max_width))
            ly = Layer(id=new_id("L"), type="text", name=self._nome(name or text.split("\n")[0][:24]),
                       text=text, style=st)
            w, h = render.text_size(ly)
            ly.x = float((self.project.width - w) / 2 if x is None else x)
            ly.y = float((self.project.height - h) / 2 if y is None else y)
            self._insert(ly, index, above)
        return self.layer_view(ly)

    def add_shape(self, kind: str = "rect", x: float = 0, y: float = 0, w: float = 200, h: float = 200,
                  fill: str | None = "#ffffff", stroke: str | None = None, stroke_width: float = 0,
                  radius: float = 0, points: list | None = None, name: str | None = None,
                  index: int | None = None, above: str | None = None) -> dict:
        if kind not in SHAPE_KINDS:
            raise EditError(f"kind 必须为以下选项之一：{', '.join(SHAPE_KINDS)}")
        if w <= 0 or h <= 0:
            raise EditError("宽度 w 和高度 h 必须为正数")
        for c in (fill, stroke):
            if c:
                fx.parse_color(c)
        with self._edit():
            sh = Shape(kind=kind, w=float(w), h=float(h), fill=fill, stroke=stroke,
                       stroke_width=float(stroke_width), radius=float(radius),
                       points=[[float(a), float(b)] for a, b in points] if points else None)
            ly = Layer(id=new_id("L"), type="shape", name=self._nome(name or {
                "rect": "矩形", "ellipse": "椭圆", "line": "直线", "polygon": "多边形"}[kind]),
                       x=float(x), y=float(y), shape=sh)
            self._insert(ly, index, above)
        return self.layer_view(ly)

    def add_fill(self, kind: str = "solid", color: str = "#000000", color2: str = "#ffffff",
                 angle: float = 90, cx: float = 0.5, cy: float = 0.5, name: str | None = None,
                 index: int | None = None, above: str | None = None) -> dict:
        if kind not in FILL_KINDS:
            raise EditError(f"kind 必须为以下选项之一：{', '.join(FILL_KINDS)}")
        fx.parse_color(color)
        fx.parse_color(color2)
        with self._edit():
            ly = Layer(id=new_id("L"), type="fill", name=self._nome(name or ("渐变" if kind != "solid" else "填充")),
                       fill=Fill(kind=kind, color=color, color2=color2, angle=float(angle), cx=float(cx), cy=float(cy)))
            self._insert(ly, index, above)
        return self.layer_view(ly)

    def add_adjustment(self, type: str | None = None, params: dict | None = None,
                       effects: list[dict] | None = None, name: str | None = None,
                       index: int | None = None, above: str | None = None) -> dict:
        """创建调整图层，其滤镜作用于所有下方内容。"""
        chain = list(effects or [])
        if type:
            chain.insert(0, {"type": type, "params": params or {}})
        if not chain:
            raise EditError("请传入 type=<效果> 和 params，或 effects=[{type, params}, ...]")
        effs = []
        for e in chain:
            if fx.spec(e["type"])["kind"] != "filter":
                raise EditError(f"{e['type']} 是图层样式，不能用于调整图层")
            effs.append(Effect(type=e["type"], params=fx.validate(e["type"], e.get("params"))))
        with self._edit():
            ly = Layer(id=new_id("L"), type="adjustment", name=self._nome(name or fx.spec(chain[0]["type"])["label"]),
                       effects=effs)
            self._insert(ly, index, above)
        return self.layer_view(ly)

    def add_paint_layer(self, name: str | None = None, index: int | None = None,
                        above: str | None = None) -> dict:
        with self._edit():
            ly = Layer(id=new_id("L"), type="paint", name=self._nome(name or "绘画"))
            self._insert(ly, index, above)
        return self.layer_view(ly)

    @staticmethod
    def _punti(points: Any) -> list[list[float]]:
        if not points or not all(isinstance(p, (list, tuple)) and len(p) == 2 for p in points):
            raise EditError("points 必须为 [x, y] 坐标列表")
        return [[float(a), float(b)] for a, b in points]

    def paint_stroke(self, points: list, layer: str | None = None, color: str = "#000000",
                     size: float = 12, hardness: float = 0.8, opacity: float = 1.0,
                     erase: bool = False, clone: list | None = None, source: list | None = None,
                     heal: bool = False) -> dict:
        """在绘画图层上绘制文档像素坐标的折线笔画。

        仿制图章使用 ``source=[x,y]`` 指定首个笔画点的取样位置，
        或使用 ``clone=[dx,dy]`` 指定偏移，取样来自图层下方的全部合成内容。
        ``heal=true`` 为修复画笔：复制纹理，匹配目标区域的光线和颜色。
        """
        punti = self._punti(points)
        fx.parse_color(color)
        if not 0 <= hardness <= 1 or not 0 <= opacity <= 1 or size <= 0:
            raise EditError("硬度和不透明度范围为 0..1，大小必须大于 0")
        if source is not None:
            if not isinstance(source, (list, tuple)) or len(source) != 2:
                raise EditError("source 必须为文档像素坐标 [x, y]")
            clone = [float(source[0]) - punti[0][0], float(source[1]) - punti[0][1]]
        if clone is not None:
            if not isinstance(clone, (list, tuple)) or len(clone) != 2:
                raise EditError("clone 必须为 [dx, dy]，表示从笔画到取样源的偏移")
            clone = [float(clone[0]), float(clone[1])]
            if erase:
                raise EditError("仿制笔画不能同时执行擦除")
        elif heal:
            raise EditError("修复画笔需要取样源，请传入 source=[x, y]；污点修复请使用 inpaint 和 points")
        with self._edit():
            if layer is None:
                top = self.project.layers[-1] if self.project.layers else None
                if top is not None and top.type == "paint" and not top.locked:
                    ly = top
                else:
                    ly = Layer(id=new_id("L"), type="paint", name=self._nome("绘画"))
                    self.project.layers.append(ly)
            else:
                ly = self._layer(layer)
                if ly.type != "paint":
                    raise EditError(f"{ly.name or ly.id} 的图层类型为 {ly.type}；请在绘画图层上绘制 "
                                    "（使用 add_paint_layer，或省略 layer=）")
            ly.strokes.append(Stroke(points=punti, color=color, size=float(size), hardness=float(hardness),
                                     opacity=float(opacity), erase=bool(erase), clone=clone, heal=bool(heal)))
        return {"layer": ly.id, "strokes": len(ly.strokes)}

    def remove_stroke(self, layer: str, index: int = -1) -> dict:
        ly = self._layer(layer)
        if ly.type != "paint" or not ly.strokes:
            raise EditError("没有可移除的笔画")
        with self._edit():
            ly.strokes.pop(index)
        return {"layer": ly.id, "strokes": len(ly.strokes)}

    # ---- 图层：属性 ----------------------------------------------------
    def set_layer(self, layer: str, name: str | None = None, visible: bool | None = None,
                  locked: bool | None = None, opacity: float | None = None, blend: str | None = None,
                  clip: bool | None = None, collapsed: bool | None = None) -> dict:
        """``clip=true`` 创建剪贴蒙版，仅在下方图层有内容的区域显示。"""
        ly = self._layer(layer, edit=False)
        if ly.locked and locked is not False and any(v is not None for v in (name, opacity, blend, clip)):
            raise EditError(f"图层 {ly.name or ly.id} 已锁定")
        modi = GROUP_BLEND_MODES if ly.type == "group" else BLEND_MODES
        if blend is not None and blend not in modi:
            raise EditError(f"blend 必须为以下选项之一：{', '.join(modi)}")
        if clip and self.project.index_of(ly.id) == 0:
            raise EditError(f"{ly.name or ly.id} 没有可供剪贴的下方图层")
        if opacity is not None and not 0 <= opacity <= 1:
            raise EditError("不透明度必须在 0..1 范围内")
        with self._edit():
            if name is not None:
                ly.name = name
            if visible is not None:
                ly.visible = bool(visible)
            if locked is not None:
                ly.locked = bool(locked)
            if opacity is not None:
                ly.opacity = float(opacity)
            if blend is not None:
                ly.blend = blend
            if clip is not None:
                ly.clip = bool(clip)
            if collapsed is not None:
                ly.collapsed = bool(collapsed)
        return self.layer_view(ly)

    def set_transform(self, layer: str, x: float | None = None, y: float | None = None,
                      scale: float | None = None, rotation: float | None = None,
                      flip_h: bool | None = None, flip_v: bool | None = None,
                      crop: list | None = None, width: float | None = None, height: float | None = None) -> dict:
        """设置位置、缩放、顺时针旋转角度、翻转及源图裁剪。

        ``width``/``height`` 可直接指定在文档中的目标尺寸，保持原比例。
        """
        ly = self._layer(layer)
        if ly.type in ("fill", "adjustment", "paint", "group"):
            raise EditError(f"a {ly.type} 类型的图层覆盖整个画布，不支持变换"
                            + ("：请改为移动组内图层" if ly.type == "group" else ""))
        with self._edit():
            if crop is not None:
                if ly.type != "image":
                    raise EditError("crop 仅适用于图片图层")
                if crop:
                    a = self.project.asset(ly.asset)
                    cx, cy, cw, ch = self._check_box(crop, "crop")
                    if cx < 0 or cy < 0 or cx + cw > a.width + 0.5 or cy + ch > a.height + 0.5:
                        raise EditError(f"裁剪区域超出源图片（{a.width}x{a.height})")
                    ly.crop = [cx, cy, cw, ch]
                else:
                    ly.crop = None
            if width is not None or height is not None:
                ly_scale_1 = copy.copy(ly)
                ly_scale_1.scale = 1.0
                w1, h1 = render.content_size(ly_scale_1, self.project, self.base_dir)
                scale = (width / w1) if width is not None else (height / h1)
            if scale is not None:
                if scale <= 0:
                    raise EditError("缩放比例必须为正数")
                ly.scale = float(scale)
            if x is not None:
                ly.x = float(x)
            if y is not None:
                ly.y = float(y)
            if rotation is not None:
                ly.rotation = float(rotation) % 360
            if flip_h is not None:
                ly.flip_h = bool(flip_h)
            if flip_v is not None:
                ly.flip_v = bool(flip_v)
        return self.layer_view(ly)

    def align_layer(self, layer: str, h: str | None = "center", v: str | None = "middle",
                    margin: float = 0) -> dict:
        """相对画布对齐图层：h 为 left/center/right，v 为 top/middle/bottom，边距以像素为单位。"""
        ly = self._layer(layer)
        if h is not None and h not in ALIGN_H:
            raise EditError(f"h 必须为以下选项之一：{', '.join(ALIGN_H)}")
        if v is not None and v not in ALIGN_V:
            raise EditError(f"v 必须为以下选项之一：{', '.join(ALIGN_V)}")
        w, hh = render.content_size(ly, self.project, self.base_dir)
        W, H = self.project.width, self.project.height
        with self._edit():
            if h == "left":
                ly.x = float(margin)
            elif h == "center":
                ly.x = (W - w) / 2
            elif h == "right":
                ly.x = W - w - margin
            if v == "top":
                ly.y = float(margin)
            elif v == "middle":
                ly.y = (H - hh) / 2
            elif v == "bottom":
                ly.y = H - hh - margin
        return self.layer_view(ly)

    def set_text(self, layer: str, text: str | None = None, **style: Any) -> dict:
        ly = self._layer(layer)
        if ly.type != "text":
            raise EditError(f"{ly.name or ly.id} 不是文字图层")
        validi = set(vars(TextStyle()))
        extra = set(k for k, v in style.items() if v is not None) - validi
        if extra:
            raise EditError(f"未知文字属性 {sorted(extra)}；可用属性：{sorted(validi)}")
        with self._edit():
            if text is not None:
                if not text:
                    raise EditError("文字不能为空")
                ly.text = text
            for k, v in style.items():
                if v is None:
                    continue
                if k in ("color", "stroke_color"):
                    fx.parse_color(v)
                if k == "align" and v not in ("left", "center", "right"):
                    raise EditError("align 必须为 left、center 或 right")
                setattr(ly.style, k, type(getattr(ly.style, k))(v))
        return self.layer_view(ly)

    def set_shape(self, layer: str, **props: Any) -> dict:
        ly = self._layer(layer)
        if ly.type != "shape":
            raise EditError(f"{ly.name or ly.id} 不是形状图层")
        validi = set(vars(Shape()))
        extra = set(k for k, v in props.items() if v is not None) - validi
        if extra:
            raise EditError(f"未知形状属性 {sorted(extra)}；可用属性：{sorted(validi)}")
        with self._edit():
            for k, v in props.items():
                if v is None:
                    continue
                if k == "kind" and v not in SHAPE_KINDS:
                    raise EditError(f"kind 必须为以下选项之一：{', '.join(SHAPE_KINDS)}")
                if k in ("fill", "stroke") and v:
                    fx.parse_color(v)
                if k in ("fill", "stroke") and v in ("", "none"):
                    v = None
                setattr(ly.shape, k, v)
        return self.layer_view(ly)

    def set_fill(self, layer: str, **props: Any) -> dict:
        ly = self._layer(layer)
        if ly.type != "fill":
            raise EditError(f"{ly.name or ly.id} 不是填充图层")
        with self._edit():
            for k, v in props.items():
                if v is None:
                    continue
                if k not in vars(Fill()):
                    raise EditError(f"未知填充属性 {k}")
                if k == "kind" and v not in FILL_KINDS:
                    raise EditError(f"kind 必须为以下选项之一：{', '.join(FILL_KINDS)}")
                if k in ("color", "color2"):
                    fx.parse_color(v)
                setattr(ly.fill, k, v)
        return self.layer_view(ly)

    # ---- 图层：顺序 ----------------------------------------------------
    def move_layer(self, layer: str, index: int | None = None, to: str | None = None,
                   into: str | None = None) -> dict:
        """重排图层。``index=0`` 为底层，``to`` 为 up/down/top/bottom，在所属列表内移动。

        ``into=<组>`` 移到组内顶部或指定 ``index``；
        ``into="root"`` 移到根列表，放在原组上方。
        """
        ly = self._layer(layer, edit=False)
        src = self.project.container(ly.id)
        i = self.project.index_of(ly.id)
        if into is not None:
            if into in ("root", ""):
                dest_list = self.project.layers
                if index is not None:
                    dest = int(index)
                else:
                    # 放到包含该图层的最外层组上方。
                    cima = ly
                    while (su := self.project.parent(cima.id)) is not None:
                        cima = su
                    dest = self.project.index_of(cima.id) + 1
            else:
                g = self._layer(into, edit=False)
                if g.type != "group":
                    raise EditError(f"{g.name or g.id} 不是组")
                if g is ly or any(x is g for x in self.project.walk(ly.children)):
                    raise EditError("不能将组移入自身内部")
                dest_list = g.children
                dest = len(g.children) if index is None else int(index)
            with self._edit():
                src.pop(i)
                if dest_list is src and i < dest:
                    dest -= 1
                dest = max(0, min(dest, len(dest_list)))
                dest_list.insert(dest, ly)
            return {"order": [x.id for x in reversed(dest_list)]}
        if to is not None:
            dest = {"up": i + 1, "down": i - 1, "top": len(src) - 1, "bottom": 0}.get(to)
            if dest is None:
                raise EditError("to 必须为 up、down、top 或 bottom")
        elif index is not None:
            dest = int(index)
        else:
            raise EditError("请传入 index=、to= 或 into=")
        dest = max(0, min(dest, len(src) - 1))
        with self._edit():
            src.insert(dest, src.pop(i))
        return {"order": [x.id for x in reversed(src)]}

    def _nuovi_id(self, ly: Layer) -> None:
        ly.id = new_id("L")
        for c in ly.children:
            self._nuovi_id(c)

    def duplicate_layer(self, layer: str, name: str | None = None) -> dict:
        ly = self._layer(layer, edit=False)
        with self._edit():
            nuovo = copy.deepcopy(ly)
            self._nuovi_id(nuovo)
            nuovo.name = self._nome(name or f"{ly.name} 副本")
            nuovo.locked = False
            self.project.container(ly.id).insert(self.project.index_of(ly.id) + 1, nuovo)
        return self.layer_view(nuovo)

    def remove_layer(self, layer: str) -> dict:
        ly = self._layer(layer)
        with self._edit():
            self.project.container(ly.id).remove(ly)
        return {"removed": ly.id}

    def group_layers(self, layers: list[str] | str, name: str | None = None) -> dict:
        """将同一列表内的图层编入新组，放在原最高图层的位置。"""
        if isinstance(layers, str):
            layers = [layers]
        if not layers:
            raise EditError("请通过 layers=[ID 或名称列表] 指定图层")
        scelti = [self._layer(x, edit=False) for x in layers]
        lista = self.project.container(scelti[0].id)
        if any(self.project.container(x.id) is not lista for x in scelti):
            raise EditError("图层必须位于同一层级，即同一个组内或全部位于根层级")
        posti = sorted(self.project.index_of(x.id) for x in {id(x): x for x in scelti}.values())
        with self._edit():
            figli = [lista[k] for k in posti]
            g = Layer(id=new_id("L"), type="group", name=self._nome(name or "组"), blend="pass_through",
                      children=figli)
            for k in reversed(posti):
                lista.pop(k)
            lista.insert(posti[-1] - len(posti) + 1, g)
        return self.layer_view(g)

    def add_group(self, name: str | None = None, index: int | None = None, above: str | None = None) -> dict:
        """创建空组，可通过 move_layer(into=...) 加入图层。"""
        with self._edit():
            g = Layer(id=new_id("L"), type="group", name=self._nome(name or "组"), blend="pass_through")
            self._insert(g, index, above)
        return self.layer_view(g)

    def ungroup(self, layer: str) -> dict:
        """取消编组，将子图层放回组所在列表的原位置。"""
        g = self._layer(layer)
        if g.type != "group":
            raise EditError(f"{g.name or g.id} 不是组")
        with self._edit():
            lista = self.project.container(g.id)
            i = self.project.index_of(g.id)
            lista[i:i + 1] = g.children
        return {"ungrouped": g.id, "layers": [c.id for c in reversed(g.children)]}

    # ---- 效果 ----------------------------------------------------------
    def add_effect(self, type: str, layer: str, params: dict | None = None, index: int | None = None) -> dict:
        ly = self._layer(layer)
        sp = fx.spec(type)
        if ly.type == "adjustment" and sp["kind"] != "filter":
            raise EditError(f"{type} 是图层样式，不能用于调整图层")
        p = fx.validate(type, params)
        with self._edit():
            e = Effect(type=type, params=p)
            if index is None:
                ly.effects.append(e)
            else:
                ly.effects.insert(int(index), e)
        return {"layer": ly.id, "effects": [x.type for x in ly.effects]}

    def update_effect(self, layer: str, index: int, params: dict | None = None,
                      enabled: bool | None = None) -> dict:
        ly = self._layer(layer)
        try:
            e = ly.effects[int(index)]
        except IndexError as exc:
            raise EditError(f"效果索引 {index} 超出范围，图层中的效果数量为 {len(ly.effects)}") from exc
        upd = fx.validate(e.type, params, partial=True) if params else {}
        with self._edit():
            e.params.update(upd)
            if enabled is not None:
                e.enabled = bool(enabled)
        return {"layer": ly.id, "index": int(index), "type": e.type, "params": e.params, "enabled": e.enabled}

    def remove_effect(self, layer: str, index: int) -> dict:
        ly = self._layer(layer)
        if not -len(ly.effects) <= int(index) < len(ly.effects):
            raise EditError(f"效果索引 {index} 超出范围")
        with self._edit():
            ly.effects.pop(int(index))
            if ly.type == "adjustment" and not ly.effects:
                self.project.layers.remove(ly)
        return {"layer": ly.id, "effects": [x.type for x in ly.effects]}

    def move_effect(self, layer: str, index: int, to: int) -> dict:
        ly = self._layer(layer)
        with self._edit():
            e = ly.effects.pop(int(index))
            ly.effects.insert(int(to), e)
        return {"layer": ly.id, "effects": [x.type for x in ly.effects]}

    def apply_look(self, name: str, layer: str | None = None) -> dict:
        """应用预设风格，创建顶层调整图层或为指定图层添加效果。"""
        if name not in presets.LOOKS:
            raise EditError(f"未知风格 {name!r}；可选：{', '.join(presets.LOOKS)}")
        chain = presets.LOOKS[name]["effects"]
        if layer is None:
            return self.add_adjustment(effects=chain, name=presets.LOOKS[name]["label"])
        ly = self._layer(layer)
        with self._edit():
            for e in chain:
                ly.effects.append(Effect(type=e["type"], params=fx.validate(e["type"], e["params"])))
        return {"layer": ly.id, "effects": [x.type for x in ly.effects]}

    # ---- 蒙版 ----------------------------------------------------------
    def set_mask(self, layer: str, kind: str = "rect", box: list | None = None, asset: str | None = None,
                 path: str | None = None, invert: bool = False, feather: float = 0.0,
                 density: float = 1.0, hide_all: bool = False) -> dict:
        """``kind="paint"`` 创建可通过 mask_stroke 绘制的蒙版，初始全部显示；
        ``hide_all`` 可设为全部隐藏。"""
        ly = self._layer(layer)
        if kind not in MASK_KINDS:
            raise EditError(f"kind 必须为以下选项之一：{', '.join(MASK_KINDS)}")
        if not 0 <= density <= 1 or feather < 0:
            raise EditError("密度范围为 0..1，羽化必须大于或等于 0")
        with self._edit():
            if kind == "image":
                if path:
                    asset = self._import_one(path)
                if not asset:
                    raise EditError("图片蒙版需要 asset= 或 path=")
                self.project.asset(asset)
                ly.mask = Mask(kind="image", asset=asset, invert=bool(invert), feather=float(feather),
                               density=float(density))
            elif kind == "paint":
                ly.mask = Mask(kind="paint", base=0.0 if hide_all else 1.0, invert=bool(invert),
                               feather=float(feather), density=float(density))
            else:
                ly.mask = Mask(kind=kind, box=self._check_box(box) if box is not None else
                               [0.0, 0.0, float(self.project.width), float(self.project.height)],
                               invert=bool(invert), feather=float(feather), density=float(density))
        return self.layer_view(ly)

    def update_mask(self, layer: str, invert: bool | None = None, feather: float | None = None,
                    density: float | None = None, box: list | None = None) -> dict:
        ly = self._layer(layer)
        if ly.mask is None:
            raise EditError(f"{ly.name or ly.id} 没有蒙版")
        with self._edit():
            if invert is not None:
                ly.mask.invert = bool(invert)
            if feather is not None:
                ly.mask.feather = max(0.0, float(feather))
            if density is not None:
                ly.mask.density = float(min(1, max(0, density)))
            if box is not None:
                if ly.mask.kind == "image":
                    raise EditError("图片蒙版不支持 box 参数")
                ly.mask.box = self._check_box(box)
        return self.layer_view(ly)

    def mask_stroke(self, layer: str, points: list, mode: str = "hide", size: float = 40,
                    hardness: float = 0.6, opacity: float = 1.0) -> dict:
        """在图层蒙版上绘制，``hide`` 隐藏，``reveal`` 显示。

        支持所有蒙版，可用于细化 AI 抠图；无蒙版时创建初始全部显示的绘画蒙版。
        """
        ly = self._layer(layer)
        if mode not in ("hide", "reveal"):
            raise EditError("mode 必须为 hide 或 reveal")
        punti = self._punti(points)
        if not 0 <= hardness <= 1 or not 0 < opacity <= 1 or size <= 0:
            raise EditError("硬度范围为 0..1，不透明度范围为 (0, 1]，大小必须大于 0")
        with self._edit():
            if ly.mask is None:
                ly.mask = Mask(kind="paint")
            # 蒙版已反相时，仍保证画笔执行所指定的显示或隐藏操作。
            bianco = (mode == "reveal") != ly.mask.invert
            ly.mask.strokes.append(Stroke(points=punti, color="#ffffff" if bianco else "#000000",
                                          size=float(size), hardness=float(hardness), opacity=float(opacity)))
        return self.layer_view(ly)

    def remove_mask_stroke(self, layer: str, index: int = -1) -> dict:
        ly = self._layer(layer)
        if ly.mask is None or not ly.mask.strokes:
            raise EditError("蒙版中没有笔画")
        with self._edit():
            ly.mask.strokes.pop(int(index))
        return self.layer_view(ly)

    def remove_mask(self, layer: str) -> dict:
        ly = self._layer(layer)
        with self._edit():
            ly.mask = None
        return self.layer_view(ly)

    # ---- 画布 ----------------------------------------------------------
    def _sposta_tutto(self, dx: float, dy: float) -> None:
        for ly in self.project.walk():
            if ly.type in ("image", "text", "shape"):
                ly.x += dx
                ly.y += dy
            for st in ly.strokes + (ly.mask.strokes if ly.mask else []):
                st.points = [[px + dx, py + dy] for px, py in st.points]
            if ly.mask and ly.mask.box:
                b = ly.mask.box
                ly.mask.box = [b[0] + dx, b[1] + dy, b[2], b[3]]

    def crop_canvas(self, x: float, y: float, width: float, height: float) -> dict:
        """裁剪画布但保留完整图层，撤销时无内容损失。"""
        x, y, w, h = self._check_box([x, y, width, height])
        with self._edit():
            self._sposta_tutto(-x, -y)
            self.project.width, self.project.height = int(round(w)), int(round(h))
        return {"width": self.project.width, "height": self.project.height}

    def resize_canvas(self, width: int, height: int, anchor: str = "center") -> dict:
        """调整画布但不缩放内容，``anchor`` 可选 center、top_left、top 等。"""
        ax = {"left": 0.0, "center": 0.5, "right": 1.0}
        ay = {"top": 0.0, "middle": 0.5, "center": 0.5, "bottom": 1.0}
        parti = anchor.split("_")
        if anchor == "center":
            fx_, fy_ = 0.5, 0.5
        elif len(parti) == 2 and parti[0] in ay and parti[1] in ax:
            fy_, fx_ = ay[parti[0]], ax[parti[1]]
        elif anchor in ax:
            fx_, fy_ = ax[anchor], 0.5
        elif anchor in ay:
            fx_, fy_ = 0.5, ay[anchor]
        else:
            raise EditError("anchor 可选：center、top、bottom、left、right、top_left、top_right、bottom_left、bottom_right")
        if not (1 <= width <= 20000 and 1 <= height <= 20000):
            raise EditError("宽度和高度必须在 1 到 20000 之间")
        with self._edit():
            dx = (width - self.project.width) * fx_
            dy = (height - self.project.height) * fy_
            self._sposta_tutto(dx, dy)
            self.project.width, self.project.height = int(width), int(height)
        return {"width": self.project.width, "height": self.project.height}

    def resize_image(self, width: int | None = None, height: int | None = None) -> dict:
        """按比例缩放整个文档（画布和内容），仅指定一边尺寸。"""
        W, H = self.project.width, self.project.height
        if width is None and height is None:
            raise EditError("请传入 width 或 height")
        kx = (width / W) if width else (height / H)
        ky = (height / H) if height else kx
        if width and height and abs(kx - ky) > 1e-3:
            raise EditError("resize_image 保持比例，请仅传入一边尺寸，或使用 resize_canvas")
        k = kx
        with self._edit():
            for ly in self.project.walk():
                if ly.type in ("image", "text", "shape"):
                    ly.x *= k
                    ly.y *= k
                    ly.scale *= k
                for st in ly.strokes + (ly.mask.strokes if ly.mask else []):
                    st.points = [[px * k, py * k] for px, py in st.points]
                    st.size *= k
                    if st.clone:
                        st.clone = [v * k for v in st.clone]
                if ly.mask:
                    if ly.mask.box:
                        ly.mask.box = [v * k for v in ly.mask.box]
                    ly.mask.feather *= k
                for e in ly.effects:
                    for pk, ps in fx.spec(e.type)["params"].items():
                        if ps.get("px") and pk in e.params:
                            e.params[pk] = e.params[pk] * k
            self.project.width = max(1, int(round(W * k)))
            self.project.height = max(1, int(round(H * k)))
        return {"width": self.project.width, "height": self.project.height}

    # ---- 栅格化 --------------------------------------------------------
    def rasterize_layer(self, layer: str) -> dict:
        """将文字、形状、绘画或效果转换为像素，创建带新素材的图片图层。"""
        ly = self._layer(layer)
        if ly.type == "adjustment":
            raise EditError("调整图层没有独立像素，请使用 merge_down 向下合并或 flatten 拼合")
        img = render.layer_image(self.project, ly.id, self.base_dir)
        bbox = img.getbbox()
        if bbox is None:
            raise EditError(f"{ly.name or ly.id} 为空")
        with self._edit():
            a = self._new_asset(img.crop(bbox), f"{ly.name}（栅格化）", "raster")
            nuovo = Layer(id=ly.id, type="image", name=ly.name, visible=ly.visible, opacity=ly.opacity,
                          blend="normal" if ly.blend == "pass_through" else ly.blend, clip=ly.clip,
                          x=float(bbox[0]), y=float(bbox[1]), asset=a.id)
            self._rimpiazza(ly, nuovo)
        return self.layer_view(nuovo)

    def merge_down(self, layer: str) -> dict:
        """将当前图层与下方图层合并为图片图层。"""
        ly = self._layer(layer)
        lista = self.project.container(ly.id)
        i = self.project.index_of(ly.id)
        if i == 0:
            raise EditError("没有下方图层")
        sotto = lista[i - 1]
        if sotto.locked:
            raise EditError(f"{sotto.name or sotto.id} 已锁定")
        sub = Project(width=self.project.width, height=self.project.height, background="transparent",
                      layers=[sotto, ly], assets=self.project.assets)
        img = render.render(sub, self.base_dir, transparent=True)
        bbox = img.getbbox() or (0, 0, 1, 1)
        with self._edit():
            a = self._new_asset(img.crop(bbox), f"{sotto.name}（合并）", "merged")
            nuovo = Layer(id=sotto.id, type="image", name=sotto.name, x=float(bbox[0]), y=float(bbox[1]), asset=a.id,
                          clip=sotto.clip)
            del lista[i]
            lista[i - 1] = nuovo
        return self.layer_view(nuovo)

    def flatten(self) -> dict:
        """将所有可见图层拼合为一个图层，保留文档背景设置。"""
        if not self.project.layers:
            raise EditError("没有可拼合的图层")
        img = render.render(self.project, self.base_dir, transparent=True)
        with self._edit():
            a = self._new_asset(img, "已拼合", "flat")
            self.project.layers = [Layer(id=new_id("L"), type="image", name="已拼合", asset=a.id)]
        return self.layer_view(self.project.layers[0])

    # ---- AI 功能 -------------------------------------------------------
    def _sorgente_layer(self, ly: Layer) -> Image.Image:
        """返回图层原始内容像素（按素材裁剪），不应用变换。"""
        if ly.type != "image":
            raise EditError(f"{ly.name or ly.id} 的图层类型为 {ly.type}；请先使用 rasterize_layer 栅格化")
        img = self._asset_img(ly.asset)
        if ly.crop:
            x, y, w, h = (int(round(v)) for v in ly.crop)
            img = img.crop((x, y, x + w, y + h))
        return img

    def remove_background(self, layer: str, keep: str = "subject", model: str = "isnet-general-use",
                          backend: str = "local") -> dict:
        """将 AI 抠图结果作为图层蒙版，保留原图。

        ``backend`` 可选 local（rembg）或支持抠图的在线服务（stability）。
        """
        ly = self._layer(layer)
        if keep not in ("subject", "background"):
            raise EditError("keep 必须为 subject 或 background")
        src = self._asset_img(ly.asset) if ly.type == "image" else None
        if src is None:
            raise EditError(f"{ly.name or ly.id} 的图层类型为 {ly.type}；移除背景仅适用于图片图层")
        try:
            m = ai.subject_mask(src, model, backend=backend)
        except ai.AIError as exc:
            raise EditError(str(exc)) from exc
        with self._edit():
            a = self._new_asset(m, f"{ly.name} 主体蒙版", "mask")
            ly.mask = Mask(kind="image", asset=a.id, invert=(keep == "background"))
        return self.layer_view(ly)

    def select_subject_box(self, layer: str, model: str = "isnet-general-use") -> dict:
        """返回文档像素坐标下的主体边界，不修改文档。"""
        ly = self._layer(layer, edit=False)
        src = self._sorgente_layer(ly)
        try:
            m = ai.subject_mask(src, model)
        except ai.AIError as exc:
            raise EditError(str(exc)) from exc
        bb = m.point(lambda v: 255 if v > 127 else 0).getbbox()
        if bb is None:
            return {"found": False}
        k = ly.scale
        return {"found": True, "box": [round(ly.x + bb[0] * k, 1), round(ly.y + bb[1] * k, 1),
                                       round((bb[2] - bb[0]) * k, 1), round((bb[3] - bb[1]) * k, 1)],
                "note": "边界框未考虑旋转" if ly.rotation else ""}

    def _maschera_zona(self, box: list | None, ellipse: bool, mask_path: str | None,
                       feather: float, points: list | None = None, size: float = 30) -> Image.Image:
        W, H = self.project.width, self.project.height
        if points:
            st = Stroke(points=self._punti(points), size=float(size), hardness=1.0)
            m = Image.fromarray((render._stroke_alpha(st, W, H, 1.0) * 255).astype(np.uint8), "L")
        elif mask_path:
            m = render.load_image(mask_path)
            alpha = m.getchannel("A")
            m = alpha if alpha.getextrema()[0] < 255 else m.convert("L")
            m = m.resize((W, H), Image.Resampling.BILINEAR)
        else:
            if box is None:
                raise EditError("请传入文档像素区域 box=[x, y, w, h] 或 mask_path=")
            x, y, w, h = self._check_box(box)
            m = Image.new("L", (W, H), 0)
            (ImageDraw.Draw(m).ellipse if ellipse else ImageDraw.Draw(m).rectangle)([x, y, x + w, y + h], fill=255)
        if feather > 0:
            m = m.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(feather / 2))
        return m

    def inpaint(self, box: list | None = None, prompt: str | None = None, ellipse: bool = False,
                mask_path: str | None = None, feather: float = 6.0, backend: str = "auto",
                seed: int | None = None, name: str | None = None, points: list | None = None,
                size: float = 30) -> dict:
        """移除或重绘区域，将结果作为补丁添加到新顶层图层。

        区域可由 ``box``、``mask_path`` 或 ``points`` + ``size`` 指定，
        后者用于笔画式污点修复。不传提示词时使用 OpenCV 或扩散算法；
        传提示词时通过 ComfyUI 或在线服务生成式填充。补丁带柔边蒙版，
        可像普通图层一样移动、调透明度或删除。
        """
        zona = self._maschera_zona(box, ellipse, mask_path, feather, points, size)
        bb = zona.getbbox()
        if bb is None:
            raise EditError("要填充的区域为空")
        W, H = self.project.width, self.project.height
        # 保留目标区域周围的上下文，供填充算法理解环境。
        pad = int(max(bb[2] - bb[0], bb[3] - bb[1]) * 0.6) + 16
        cx0, cy0 = max(0, bb[0] - pad), max(0, bb[1] - pad)
        cx1, cy1 = min(W, bb[2] + pad), min(H, bb[3] + pad)
        comp = render.render(self.project, self.base_dir).convert("RGB").crop((cx0, cy0, cx1, cy1))
        m_crop = zona.crop((cx0, cy0, cx1, cy1))
        hard = m_crop.point(lambda v: 255 if v > 8 else 0)
        try:
            filled, motore = ai.fill(comp, hard, prompt, backend=backend, seed=seed)
        except ai.AIError as exc:
            raise EditError(str(exc)) from exc
        # 补丁仅保留目标区域及羽化边缘，上下文只用于生成。
        mb = m_crop.getbbox() or (0, 0, *m_crop.size)
        patch = filled.convert("RGBA").crop(mb)
        with self._edit():
            a = self._new_asset(patch, "填充补丁", "fill")
            ma = self._new_asset(m_crop.crop(mb), "填充补丁蒙版", "fillmask")
            ly = Layer(id=new_id("L"), type="image",
                       name=self._nome(name or ("生成式填充" if prompt else "修复结果" if points else "移除结果")),
                       asset=a.id, x=float(cx0 + mb[0]), y=float(cy0 + mb[1]),
                       mask=Mask(kind="image", asset=ma.id))
            self.project.layers.append(ly)
        return {**self.layer_view(ly), "backend": motore}

    def generate_image(self, prompt: str, width: int | None = None, height: int | None = None,
                       negative: str | None = None, seed: int | None = None, fit: str = "cover",
                       name: str | None = None, backend: str = "auto") -> dict:
        """通过 ComfyUI 或在线服务文生图，并添加为新图层。"""
        if not prompt:
            raise EditError("提示词不能为空")
        W, H = self.project.width, self.project.height
        k = 1024 / max(W, H)
        gw, gh = width or int(W * k), height or int(H * k)
        try:
            img, motore = ai.generate(prompt, gw, gh, negative, seed, backend=backend)
        except (ai.AIError, RuntimeError) as exc:
            raise EditError(str(exc)) from exc
        with self._edit():
            a = self._new_asset(img, prompt[:40], "gen")
            out = self._add_image_inside(a, fit, name or "生成图片")
        return {**out, "backend": motore}

    def edit_image(self, prompt: str, layer: str | None = None, box: list | None = None,
                   strength: float = 0.6, backend: str = "auto", seed: int | None = None,
                   name: str | None = None) -> dict:
        """图生图：按提示词重绘图层或图片区域。

        在原图上方创建位置和蒙版相同的新图层，保留原图。
        隐藏新图层可比较前后效果，调整不透明度可控制强度。
        """
        if not prompt:
            raise EditError("提示词不能为空")
        if not 0 < strength <= 1:
            raise EditError("强度必须在 (0, 1] 范围内，低值保留原图，高值增加改动")
        if layer is not None:
            ly = self._layer(layer, edit=False)
            src = self._sorgente_layer(ly)
        else:
            ly = None
            x0, y0, x1, y1 = self._clip_box(self._check_box(box)) if box is not None else (
                0, 0, self.project.width, self.project.height)
            src = render.render(self.project, self.base_dir).crop((x0, y0, x1, y1))
        try:
            out, motore = ai.edit(src.convert("RGB"), prompt, strength, seed, backend=backend)
        except (ai.AIError, RuntimeError) as exc:
            raise EditError(str(exc)) from exc
        with self._edit():
            if ly is not None:
                res = out.convert("RGBA")
                if src.mode == "RGBA":
                    res.putalpha(src.getchannel("A"))
                a = self._new_asset(res, f"{ly.name} AI 编辑", "edit")
                nuovo = Layer(id=new_id("L"), type="image", name=self._nome(name or f"{ly.name}（AI 编辑）"),
                              asset=a.id, x=ly.x, y=ly.y, scale=ly.scale, rotation=ly.rotation,
                              flip_h=ly.flip_h, flip_v=ly.flip_v, mask=copy.deepcopy(ly.mask), clip=ly.clip)
                if nuovo.mask and nuovo.mask.kind == "image" and ly.crop:
                    # 原蒙版与完整素材对齐，生成结果则与裁剪区域对齐。
                    m = self._asset_img(ly.mask.asset)
                    cx, cy, cw, ch = (int(round(v)) for v in ly.crop)
                    sa = self.project.asset(ly.asset)
                    m = m.resize((sa.width, sa.height)).crop((cx, cy, cx + cw, cy + ch))
                    nuovo.mask.asset = self._new_asset(m, f"{ly.name} 蒙版", "mask").id
                self._insert(nuovo, above=ly.id)
            else:
                a = self._new_asset(out.convert("RGBA"), prompt[:40], "edit")
                nuovo = Layer(id=new_id("L"), type="image", name=self._nome(name or "AI 编辑"), asset=a.id,
                              x=float(x0), y=float(y0))
                self.project.layers.append(nuovo)
        return {**self.layer_view(nuovo), "backend": motore}

    def describe_image(self, layer: str | None = None, box: list | None = None, question: str | None = None,
                       backend: str = "auto") -> dict:
        """使用视觉模型描述整张图片、图层或区域。"""
        if layer is not None:
            img = render.layer_image(self.project, self._layer(layer, edit=False).id, self.base_dir)
            bb = img.getbbox()
            if bb:
                img = img.crop(bb)
            bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
            img = Image.alpha_composite(bg, img.convert("RGBA"))
        else:
            img = render.render(self.project, self.base_dir)
            if box is not None:
                img = img.crop(self._clip_box(self._check_box(box)))
        try:
            testo, motore = ai.describe(img.convert("RGB"), question, backend=backend)
        except (ai.AIError, RuntimeError) as exc:
            raise EditError(str(exc)) from exc
        return {"text": testo, "backend": motore}

    def expand_canvas(self, left: float = 0, top: float = 0, right: float = 0, bottom: float = 0,
                      prompt: str | None = None, backend: str = "auto", seed: int | None = None,
                      feather: float = 24.0) -> dict:
        """扩展画布并填充新增边缘。

        未提供提示词和默认在线服务时，使用传统填充，适合天空、墙面等平滑背景。
        填充结果以新顶层图层保存，蒙版仅覆盖新增区域。结果来自包含调整效果的
        合成图，因此放在调整层上方，避免重复应用调整。原始图层保持完整。
        """
        l, t, r, b = (int(round(float(v))) for v in (left, top, right, bottom))
        if min(l, t, r, b) < 0 or l + t + r + b == 0:
            raise EditError("请通过 left、top、right、bottom 指定各边增加的像素，数值必须大于或等于 0")
        W0, H0 = self.project.width, self.project.height
        W, H = W0 + l + r, H0 + t + b
        if W > 20000 or H > 20000:
            raise EditError("扩展后的画布将超过 20000 像素")
        testo = prompt or ("continue the scene seamlessly" if backend not in ai.LOCALI else None)
        with self.batch():
            self.project.width, self.project.height = W, H
            self._sposta_tutto(l, t)
            comp = render.render(self.project, self.base_dir).convert("RGB")
            nuova = Image.new("L", (W, H), 255)
            nuova.paste(0, (l, t, l + W0, t + H0))
            try:
                filled, motore = ai.fill(comp, nuova, testo, backend=backend, seed=seed)
            except ai.AIError as exc:
                # batch 在失败时恢复原文档。
                raise EditError(str(exc)) from exc
            # 补丁略微覆盖原图边缘并羽化，避免接缝。
            m = nuova.filter(ImageFilter.MaxFilter(3))
            if feather > 0:
                m = m.filter(ImageFilter.MaxFilter(int(feather) // 2 * 2 + 1)).filter(ImageFilter.GaussianBlur(feather / 2))
            a = self._new_asset(filled.convert("RGBA"), "扩图填充", "expand")
            ma = self._new_asset(m, "扩图蒙版", "expandmask")
            ly = Layer(id=new_id("L"), type="image", name=self._nome("扩图结果"), asset=a.id,
                       mask=Mask(kind="image", asset=ma.id))
            self.project.layers.append(ly)
        return {"width": W, "height": H, "layer": ly.id, "backend": motore}

    def _add_image_inside(self, a: Asset, fit: str, name: str) -> dict:
        W, H = self.project.width, self.project.height
        scale = max(W / a.width, H / a.height) if fit == "cover" else min(W / a.width, H / a.height)
        ly = Layer(id=new_id("L"), type="image", name=self._nome(name), asset=a.id, scale=scale,
                   x=(W - a.width * scale) / 2, y=(H - a.height * scale) / 2)
        self.project.layers.append(ly)
        return self.layer_view(ly)

    def upscale(self, layer: str, factor: float = 2.0, backend: str = "auto") -> dict:
        """增加图片图层的实际像素，保持其在文档中的显示尺寸。"""
        ly = self._layer(layer)
        if not 1 < factor <= 8:
            raise EditError("放大倍数必须在 (1, 8] 范围内")
        src = self._sorgente_layer(ly)
        try:
            up, motore = ai.upscale(src, factor, backend)
        except (ai.AIError, RuntimeError) as exc:
            raise EditError(str(exc)) from exc
        k = up.width / src.width
        with self._edit():
            a = self._new_asset(up, f"{ly.name} x{factor:g}", "up")
            if ly.mask and ly.mask.kind == "image":
                # 将原本对齐源素材的蒙版重新对齐到新素材。
                m = self._asset_img(ly.mask.asset)
                if ly.crop:
                    x, y, w, h = (int(round(v)) for v in ly.crop)
                    m = m.crop((x, y, x + w, y + h))
                ma = self._new_asset(m.resize(up.size, Image.Resampling.BILINEAR), f"{ly.name} 蒙版", "mask")
                ly.mask.asset = ma.id
            ly.asset, ly.crop = a.id, None
            ly.scale = ly.scale / k
        return {**self.layer_view(ly), "backend": motore, "pixels": [up.width, up.height]}

    def auto_enhance(self, layer: str | None = None, strength: float = 1.0) -> dict:
        """测量图片并添加普通修正滤镜，或创建顶层调整图层。"""
        if not 0 < strength <= 2:
            raise EditError("强度必须在 (0, 2] 范围内")
        s = min(1.0, 1200 / max(self.project.width, 1))
        if layer is None:
            arr = render.composite(self.project, self.base_dir, scale=s)
        else:
            ly = self._layer(layer)
            arr = fx.to_np(render.layer_image(self.project, ly.id, self.base_dir, scale=s))
        chain = analyze.suggest_enhance(arr, strength)
        if not chain:
            return {"changed": False, "note": "图片已达到均衡状态"}
        if layer is None:
            out = self.add_adjustment(effects=chain, name="自动增强")
        else:
            ly = self._layer(layer)
            with self._edit():
                for e in chain:
                    ly.effects.append(Effect(type=e["type"], params=fx.validate(e["type"], e["params"])))
            out = self.layer_view(ly)
        return {**out, "applied": chain}

    def match_color(self, layer: str, reference: str, amount: float = 1.0) -> dict:
        """通过统计颜色迁移，让 ``layer`` 的色彩和色调接近 ``reference``。"""
        ly = self._layer(layer)
        ref = self._layer(reference, edit=False)
        s = min(1.0, 800 / max(self.project.width, 1))
        a_src = fx.to_np(render.layer_image(self.project, ly.id, self.base_dir, scale=s))
        a_ref = fx.to_np(render.layer_image(self.project, ref.id, self.base_dir, scale=s))
        sm, ss = analyze.channel_stats(a_src)
        dm, ds = analyze.channel_stats(a_ref)
        params = fx.validate("color_match", {"src_mean": sm, "src_std": ss, "dst_mean": dm,
                                             "dst_std": ds, "amount": amount})
        with self._edit():
            ly.effects = [e for e in ly.effects if e.type != "color_match"]
            ly.effects.append(Effect(type="color_match", params=params))
        return self.layer_view(ly)

    # ---- 导出 ----------------------------------------------------------
    def export_image(self, path: str, quality: int = 92, scale: float = 1.0, transparent: bool = False,
                     box: list | None = None) -> dict:
        if not 0 < scale <= 4:
            raise EditError("缩放比例必须在 (0, 4] 范围内")
        img = render.render(self.project, self.base_dir, scale=scale, transparent=transparent)
        if box is not None:
            x0, y0, x1, y1 = self._clip_box(self._check_box(box))
            img = img.crop((int(x0 * scale), int(y0 * scale), int(x1 * scale), int(y1 * scale)))
        try:
            out = render.save_image(img, path, quality=quality,
                                    background=self.project.background if not transparent else "transparent",
                                    dpi=self.project.dpi)
        except ValueError as exc:
            raise EditError(str(exc)) from exc
        return {"path": str(out), "width": img.width, "height": img.height, "bytes": out.stat().st_size}


def asarray(img: Image.Image) -> np.ndarray:
    return fx.to_np(img)
