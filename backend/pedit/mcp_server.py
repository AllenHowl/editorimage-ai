"""MCP 服务：将编辑器操作作为工具提供给智能体。

与 vedit 保持相同原则：
- 每个工具均调用 ``Store`` 方法，与界面共用能力。
- 返回无多余空格的紧凑 JSON，减少上下文占用。
- 错误信息说明合法输入及修正方法。
- ``preview`` 返回真实渲染图片，让智能体直接检查结果。

运行 ``pedit-mcp`` 启动 stdio 服务；``pedit ui`` 中同一服务挂载在
``/mcp/``，操作浏览器内打开的文档。
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

ISTRUZIONI = """类似 Photoshop/GIMP 的非破坏式图层图片编辑器。

流程：project_create（或 project_open 打开照片）→ add_image / add_text /
add_shape / add_fill → 调整（add_adjustment、apply_look、auto_enhance）→
修图（add_effect、set_mask、inpaint、remove_background）→ preview 查看
→ review_image 检查 → export_image（仅在用户要求时）。

坐标使用文档像素，原点在左上角。x/y 为内容左上角，rotation 为顺时针角度。
project_info 中图层从上到下排列，可通过 ID 或名称指定图层。

保持非破坏式编辑：
- 滤镜保存在 effects 中，可通过 update_effect 修改或禁用。
- 整图修正使用 add_adjustment / apply_look 创建顶层调整图层。
- 隐藏部分内容使用 set_mask，保留像素。
- remove_background 创建蒙版，inpaint 在新图层中添加补丁，保留原图并支持撤销。

第一原则：查看结果。
preview 显示真实文档，grid=true 提供像素坐标，决定位置和边界前先查看。
box 可放大细节，每组修改后检查结果。analyze_image 提供曝光、溢出像素、
偏色和清晰度等测量，修正前先测量，避免盲目调亮过曝图片。

修图原则：
1. 先修正曝光和白平衡（auto_enhance 或 levels/temperature），再应用风格。
2. 适度调整：饱和度超过 +0.4、对比度超过 +0.5、锐化超过 1.5 容易失真，
   使用 preview 和 box 放大检查。
3. 文字需与背景保持对比，使用 review_image 测量、align_layer 的 margin 留边。
   复杂背景可添加 stroke、drop_shadow 或底部形状。
4. 构图使用 align_layer，保持三分法及一致边距。
5. AI：remove_background 抠图；无提示词的 inpaint 移除物体，points 用于污点修复；
   带提示词的 inpaint、generate_image、edit_image、expand_canvas、upscale
   可用 ComfyUI 或在线服务（backend=openai、gemini、stability、openrouter、replicate）。
   describe_image 描述图片。先用 ai_status 确认能力，再承诺执行。在线服务会产生费用，
   backend="auto" 仅使用用户已选的默认服务；仅在用户要求时显式指定其他在线服务。
6. 图层组使用 group_layers / move_layer(into=<组>)；剪贴蒙版使用 set_layer(clip=true)；
   mask_stroke 通过 hide/reveal 绘制或细化蒙版；paint_stroke(source=[x,y])
   为仿制图章，heal=true 为修复画笔。

节省调用：batch 合并已确定的修改，整体只需一步撤销；look=true 同时返回预览。
仅在不清楚状态时调用 project_info。

禁止自行导出：仅在用户要求时调用 export_image。用户想查看或手动修改时，
open_ui 在同一文档上打开界面。发现故障、不便或缺失功能时，用 report_issue 记录后继续。
默认使用简体中文回答，用户明确指定其他语言时遵从其要求。
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
    """移除 pydantic 结构中重复的 ``title`` 和 ``anyOf [x, null]``。"""
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


# 工具名到函数的映射，供 ``batch`` 和聊天助手使用。
STRUMENTI: dict[str, Any] = {}
# 不适合放入 batch 的工具：预览、导出和界面控制。
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
    """将大图渲染、AI 等阻塞任务移出事件循环，并定期报告进度。

    Claude Code 会中断连续 300 秒没有输出的调用，小显卡上的
    ComfyUI 生成任务可能达到这个时长。
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
# 当前文档
# --------------------------------------------------------------------------

_stores: dict[str, Store] = {}
_current: list[Store | None] = [None]


def _ospite() -> Store | None:
    """服务在界面进程内运行时，返回界面当前打开的文档。"""
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
        raise EditError("尚未打开文档，请使用 project_create 或 project_open")
    return _current[0]


def _png(img: PILImage.Image) -> Image:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=False)
    return Image(data=buf.getvalue(), format="png")


def _griglia(img: PILImage.Image, W: int, H: int, box: list[float] | None = None) -> PILImage.Image:
    """绘制间距为 10% 的网格，标注文档像素坐标以辅助定位。"""
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
# 文档
# --------------------------------------------------------------------------


@_strumento()
def project_create(path: str | None = None, name: str = "未命名", preset: str | None = None,
                   width: int | None = None, height: int | None = None, background: str = "#ffffff",
                   from_image: str | None = None) -> dict:
    """创建文档。preset 可选 1080p、4k、square、instagram_post、story、youtube_thumbnail、
    a4_300dpi、a4_150dpi、web_banner、og_image，也可指定 width/height；
    from_image=<文件> 从照片开始，画布与照片同尺寸。path=<文件.json> 保存文档并在每次
    编辑后自动保存。background 可设为 'transparent'（透明）。"""
    s = Store.create(name=name, preset=preset, width=width, height=height, background=background,
                     path=path, from_image=from_image)
    if path:
        _stores[s.path] = s
    _current[0] = s
    _ui_attach(s)
    return s.summary()


@_strumento()
def project_open(path: str) -> dict:
    """打开 pedit .json 文档或照片；照片会成为同尺寸、尚未保存的新文档。"""
    s = Store.open(path)
    if s.path:
        _stores[s.path] = s
    _current[0] = s
    _ui_attach(s)
    return s.summary()


@_strumento()
def project_info(detail: str = "normal", project: str | None = None) -> dict:
    """返回文档尺寸、从上到下的图层及其 ID、边界、效果和蒙版。detail='full' 包含全部参数。"""
    return _store(project).summary(detail)


@_strumento()
def project_save(path: str | None = None, project: str | None = None) -> dict:
    """保存 JSON 文档；提供 path 时执行另存为。"""
    return {"path": _store(project).save(path)}


@_strumento()
def project_settings(name: str | None = None, background: str | None = None, dpi: int | None = None,
                     project: str | None = None) -> dict:
    """设置文档名称、背景颜色和 DPI；背景支持 'transparent'。"""
    return _store(project).set_document(name, background, dpi)


@_strumento()
def undo(project: str | None = None) -> dict:
    """撤销上一次编辑。"""
    return _store(project).undo()


@_strumento()
def redo(project: str | None = None) -> dict:
    """重做上一次撤销的编辑。"""
    return _store(project).redo()


# --------------------------------------------------------------------------
# 图层
# --------------------------------------------------------------------------


@_strumento()
def add_image(path: str | None = None, asset: str | None = None, x: float | None = None,
              y: float | None = None, scale: float | None = None, fit: str = "contain",
              name: str | None = None, above: str | None = None, project: str | None = None) -> dict:
    """从 path 文件或已导入素材创建图片图层。fit 可选 contain、cover、width、height、none；
    提供 scale 时忽略 fit。不传 x/y 时居中；above=<图层> 将新图层插到其正上方。"""
    return _store(project).add_image(asset=asset, path=path, x=x, y=y, scale=scale, fit=fit, name=name, above=above)


@_strumento()
def add_text(text: str, x: float | None = None, y: float | None = None, size: float = 64,
             color: str = "#ffffff", font: str = "sans", bold: bool = False, italic: bool = False,
             align: str = "left", line_spacing: float = 1.2, stroke_width: float = 0,
             stroke_color: str = "#000000", max_width: float = 0, name: str | None = None,
             project: str | None = None) -> dict:
    """创建文字图层。font 可选 sans、serif、mono、display、handwriting，或系统字体名/文件路径。
    max_width>0 时自动换行。不传 x/y 时居中，之后可用 align_layer 调整位置。"""
    return _store(project).add_text(text, x, y, size, color, font, bold, italic, align, line_spacing,
                                    stroke_width, stroke_color, max_width, name)


@_strumento()
def add_shape(kind: str = "rect", x: float = 0, y: float = 0, w: float = 200, h: float = 200,
              fill: str | None = "#ffffff", stroke: str | None = None, stroke_width: float = 0,
              radius: float = 0, points: list | None = None, name: str | None = None,
              project: str | None = None) -> dict:
    """创建矢量形状：rect（radius 控制圆角）、ellipse、line、polygon（points 相对 x,y）。
    颜色支持 #rrggbbaa 格式以设置透明度。"""
    return _store(project).add_shape(kind, x, y, w, h, fill, stroke, stroke_width, radius, points, name)


@_strumento()
def add_fill(kind: str = "solid", color: str = "#000000", color2: str = "#ffffff", angle: float = 90,
             cx: float = 0.5, cy: float = 0.5, name: str | None = None, index: int | None = None,
             project: str | None = None) -> dict:
    """创建全画布填充：solid 纯色、linear 线性渐变（angle=90 从左到右，180 从上到下）或
    radial 径向渐变（cx、cy 为相对画布的中心比例）。index=0 放在最底层作为背景。"""
    return _store(project).add_fill(kind, color, color2, angle, cx, cy, name, index)


@_strumento()
def add_adjustment(type: str | None = None, params: dict | None = None, effects: list[dict] | None = None,
                   name: str | None = None, project: str | None = None) -> dict:
    """在顶层创建调整图层，以非破坏方式影响下方内容。可传入单个滤镜 type + params，
    或效果链 effects=[{type, params}, ...]。可用效果参见 list_effects。"""
    return _store(project).add_adjustment(type, params, effects, name)


@_strumento()
def paint_stroke(points: list, layer: str | None = None, color: str = "#000000", size: float = 12,
                 hardness: float = 0.8, opacity: float = 1.0, erase: bool = False, source: list | None = None,
                 heal: bool = False, project: str | None = None) -> dict:
    """在绘画图层上添加笔画，points 为文档像素坐标的折线 [[x,y],...]，缺少图层时自动创建。
    erase=true 擦除该层已有笔画。仿制图章使用 source=[x,y] 指定首点的取样位置，
    取样自图层下方的合成内容；heal=true 启用修复画笔，匹配目标区域的光线和颜色。"""
    return _store(project).paint_stroke(points, layer, color, size, hardness, opacity, erase, source=source,
                                        heal=heal)


@_strumento()
def set_layer(layer: str, name: str | None = None, visible: bool | None = None, locked: bool | None = None,
              opacity: float | None = None, blend: str | None = None, clip: bool | None = None,
              project: str | None = None) -> dict:
    """设置图层名称、可见性、锁定、不透明度 0..1、clip（剪贴到下方图层）及混合模式。
    模式可选 normal、dissolve、darken、multiply、color_burn、linear_burn、darker_color、
    lighten、screen、color_dodge、add（线性减淡）、lighter_color、overlay、soft_light、
    hard_light、vivid_light、linear_light、pin_light、hard_mix、difference、exclusion、
    subtract、divide、hue、saturation、color、luminosity；组还支持 pass_through。"""
    return _store(project).set_layer(layer, name, visible, locked, opacity, blend, clip)


@_strumento()
def set_transform(layer: str, x: float | None = None, y: float | None = None, scale: float | None = None,
                  rotation: float | None = None, flip_h: bool | None = None, flip_v: bool | None = None,
                  crop: list | None = None, width: float | None = None, height: float | None = None,
                  project: str | None = None) -> dict:
    """移动、缩放、顺时针旋转（角度）或翻转图层。width 或 height 设置画布上的目标尺寸并保持比例。
    crop=[x,y,w,h] 使用图片图层的源像素坐标，传 [] 可移除裁剪。"""
    return _store(project).set_transform(layer, x, y, scale, rotation, flip_h, flip_v, crop, width, height)


@_strumento()
def align_layer(layer: str, h: str | None = "center", v: str | None = "middle", margin: float = 0,
                project: str | None = None) -> dict:
    """相对画布对齐：h 为 left/center/right，v 为 top/middle/bottom；null 保持原值，margin 单位为像素。"""
    return _store(project).align_layer(layer, h, v, margin)


@_strumento()
def set_text(layer: str, text: str | None = None, font: str | None = None, size: float | None = None,
             color: str | None = None, bold: bool | None = None, italic: bool | None = None,
             align: str | None = None, line_spacing: float | None = None, stroke_width: float | None = None,
             stroke_color: str | None = None, max_width: float | None = None,
             project: str | None = None) -> dict:
    """修改文字图层的文字内容或样式。"""
    return _store(project).set_text(layer, text, font=font, size=size, color=color, bold=bold, italic=italic,
                                    align=align, line_spacing=line_spacing, stroke_width=stroke_width,
                                    stroke_color=stroke_color, max_width=max_width)


@_strumento()
def set_shape(layer: str, kind: str | None = None, w: float | None = None, h: float | None = None,
              fill: str | None = None, stroke: str | None = None, stroke_width: float | None = None,
              radius: float | None = None, points: list | None = None, project: str | None = None) -> dict:
    """修改形状图层；fill/stroke 设为 'none' 可移除填充或描边。"""
    return _store(project).set_shape(layer, kind=kind, w=w, h=h, fill=fill, stroke=stroke,
                                     stroke_width=stroke_width, radius=radius, points=points)


@_strumento()
def set_fill(layer: str, kind: str | None = None, color: str | None = None, color2: str | None = None,
             angle: float | None = None, cx: float | None = None, cy: float | None = None,
             project: str | None = None) -> dict:
    """修改纯色或渐变填充图层。"""
    return _store(project).set_fill(layer, kind=kind, color=color, color2=color2, angle=angle, cx=cx, cy=cy)


@_strumento()
def move_layer(layer: str, to: str | None = None, index: int | None = None, into: str | None = None,
               project: str | None = None) -> dict:
    """在当前列表内重排图层：to=up/down/top/bottom，或传 index（0 为底层）。
    into=<组> 移入该组，into="root" 移出当前组。"""
    return _store(project).move_layer(layer, index, to, into)


@_strumento()
def group_layers(layers: list[str], name: str | None = None, project: str | None = None) -> dict:
    """将同一层级的图层编入新组。组拥有独立的不透明度、混合模式（默认 pass_through）、
    蒙版和效果；非穿透组内的调整仅影响该组。"""
    return _store(project).group_layers(layers, name)


@_strumento()
def ungroup(layer: str, project: str | None = None) -> dict:
    """取消编组，将子图层移回组所在的位置。"""
    return _store(project).ungroup(layer)


@_strumento()
def duplicate_layer(layer: str, name: str | None = None, project: str | None = None) -> dict:
    """复制图层，将副本放在原图层正上方。"""
    return _store(project).duplicate_layer(layer, name)


@_strumento()
def remove_layer(layer: str, project: str | None = None) -> dict:
    """删除图层，可撤销。"""
    return _store(project).remove_layer(layer)


@_strumento()
def rasterize_layer(layer: str, project: str | None = None) -> dict:
    """将文字、形状或绘画图层及其效果和蒙版栅格化为像素。"""
    return _store(project).rasterize_layer(layer)


@_strumento()
def merge_down(layer: str, project: str | None = None) -> dict:
    """将图层合并到下方图层。"""
    return _store(project).merge_down(layer)


@_strumento()
def flatten(project: str | None = None) -> dict:
    """将所有可见图层拼合为一个图片图层。"""
    return _store(project).flatten()


# --------------------------------------------------------------------------
# 效果和蒙版
# --------------------------------------------------------------------------


@_strumento()
def list_effects(name: str | None = None) -> list | dict:
    """效果目录。不传 name 时返回名称和简要说明；指定 name 时返回其参数。"""
    if name:
        sp = fx.spec(name)
        return {"name": name, "kind": sp["kind"], "desc": sp["desc"],
                "params": {k: {kk: vv for kk, vv in v.items() if kk != "px" or vv} for k, v in sp["params"].items()}}
    return [f"{e['name']} ({e['kind']}): {e['desc']}" for e in fx.describe()]


@_strumento()
def add_effect(type: str, layer: str, params: dict | None = None, index: int | None = None,
               project: str | None = None) -> dict:
    """为图层添加滤镜或样式。滤镜在蒙版前应用；阴影、描边、发光等样式在蒙版后应用。"""
    return _store(project).add_effect(type, layer, params, index)


@_strumento()
def update_effect(layer: str, index: int, params: dict | None = None, enabled: bool | None = None,
                  project: str | None = None) -> dict:
    """修改效果参数或启用状态。index 为该效果在图层效果列表中的位置。"""
    return _store(project).update_effect(layer, index, params, enabled)


@_strumento()
def remove_effect(layer: str, index: int, project: str | None = None) -> dict:
    """移除图层的一个效果。"""
    return _store(project).remove_effect(layer, index)


@_strumento()
def move_effect(layer: str, index: int, to: int, project: str | None = None) -> dict:
    """调整效果顺序，顺序会影响结果，例如应先降噪再锐化。"""
    return _store(project).move_effect(layer, index, to)


@_strumento()
def list_looks() -> list:
    """列出供 apply_look 使用的预设风格。"""
    return [f"{x['name']}: {x['desc']}" for x in presets.describe()]


@_strumento()
def apply_look(name: str, layer: str | None = None, project: str | None = None) -> dict:
    """应用风格，默认创建顶层调整图层，也可作为效果添加到指定图层。"""
    return _store(project).apply_look(name, layer)


@_strumento()
def set_mask(layer: str, kind: str = "rect", box: list | None = None, path: str | None = None,
             invert: bool = False, feather: float = 0, density: float = 1.0, hide_all: bool = False,
             project: str | None = None) -> dict:
    """创建图层蒙版，白色可见。kind 为 rect/ellipse 时，box=[x,y,w,h] 使用文档像素；
    image 通过 path 指定灰度或带 alpha 的图片，与图层内容对齐；paint 可通过 mask_stroke
    绘制，hide_all=true 使初始内容全部隐藏。feather 为柔边宽度，单位为像素。"""
    return _store(project).set_mask(layer, kind, box, None, path, invert, feather, density, hide_all)


@_strumento()
def mask_stroke(layer: str, points: list, mode: str = "hide", size: float = 40, hardness: float = 0.6,
                opacity: float = 1.0, project: str | None = None) -> dict:
    """在图层蒙版上绘制，mode 可选 hide 或 reveal，points 为文档像素坐标折线 [[x,y],...]。
    支持细化任意蒙版（包括 AI 抠图），无蒙版时自动创建绘画蒙版。"""
    return _store(project).mask_stroke(layer, points, mode, size, hardness, opacity)


@_strumento()
def update_mask(layer: str, invert: bool | None = None, feather: float | None = None,
                density: float | None = None, box: list | None = None, project: str | None = None) -> dict:
    """反相、羽化、减弱或移动现有蒙版。"""
    return _store(project).update_mask(layer, invert, feather, density, box)


@_strumento()
def remove_mask(layer: str, project: str | None = None) -> dict:
    """移除图层蒙版。"""
    return _store(project).remove_mask(layer)


# --------------------------------------------------------------------------
# 画布
# --------------------------------------------------------------------------


@_strumento()
def crop_canvas(x: float, y: float, width: float, height: float, project: str | None = None) -> dict:
    """按文档像素边界裁剪画布，保留完整图层，可无损撤销。"""
    return _store(project).crop_canvas(x, y, width, height)


@_strumento()
def resize_canvas(width: int, height: int, anchor: str = "center", project: str | None = None) -> dict:
    """调整画布尺寸，不缩放内容。anchor 可选 center、top、bottom_left 等。"""
    return _store(project).resize_canvas(width, height, anchor)


@_strumento()
async def expand_canvas(left: float = 0, top: float = 0, right: float = 0, bottom: float = 0,
                        prompt: str | None = None, backend: str = "auto", seed: int | None = None,
                        project: str | None = None) -> dict:
    """生成式扩图：指定四边增加的像素，填充结果保存为带区域蒙版的新顶层图层。
    不传提示词时使用传统填充，适合天空、墙面、纯色背景；传提示词或使用在线 AI 服务时生成新内容。"""
    s = _store(project)
    return await _off(s.expand_canvas, left, top, right, bottom, prompt, backend, seed)


@_strumento()
def resize_image(width: int | None = None, height: int | None = None, project: str | None = None) -> dict:
    """按比例缩放整个文档（画布和内容），只需指定一边尺寸。"""
    return _store(project).resize_image(width, height)


# --------------------------------------------------------------------------
# 查看和测量
# --------------------------------------------------------------------------


@_strumento()
async def preview(width: int = 1024, box: list | None = None, layer: str | None = None, grid: bool = False,
                  project: str | None = None) -> list:
    """查看真实渲染结果。box=[x,y,w,h] 可放大文档中的细节区域；layer=<ID> 单独查看图层；
    grid=true 叠加 10% 网格并标注文档像素坐标。"""
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
    return [_png(img), f"{p.width}x{p.height} 文档，预览尺寸 {img.width}x{img.height}"]


@_strumento()
async def analyze_image(layer: str | None = None, box: list | None = None, project: str | None = None) -> dict:
    """测量整张图片、单个图层或区域的亮度、对比度、阴影/高光溢出、饱和度、偏色、清晰度和主色。
    修正前先测量。"""
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
    """检查文字超出画布、文字对比度不足、图层越界、高光过曝和图片放大等问题。
    每条警告提供可修复该问题的工具；仍有警告时应继续处理。"""
    s = _store(project)
    avvisi = await _off(analyze.review, s.project, s.base_dir)
    return {"ok": not avvisi, "warnings": avvisi}


# --------------------------------------------------------------------------
# AI 功能
# --------------------------------------------------------------------------


@_strumento()
async def ai_status() -> dict:
    """查询可用 AI：抠图、填充引擎、ComfyUI（生成和放大）、已配置密钥的在线服务及默认服务。
    online.ready 为服务到支持任务的映射。"""
    return await _off(ai.status)


@_strumento()
def ai_services(service: str | None = None, models: dict | None = None, url: str | None = None,
                default: bool | None = None) -> dict:
    """查询在线 AI 服务（OpenAI、Gemini、Stability、OpenRouter、Replicate、Anthropic、Ollama、custom）
    的可用状态、任务（generate/edit/inpaint/describe/upscale/cutout）和模型。指定 service 可设置
    models={任务: 模型}、url 或 default=true，供 backend="auto" 使用。
    API 密钥由用户在界面的“AI 模型”中粘贴或设为环境变量，不通过此工具传入。"""
    if service:
        return cloud.imposta(service, None, models, url, default)
    st = cloud.stato()
    return {"default": st["default"] or None,
            "services": [{k: x[k] for k in ("id", "pronto", "fa", "modelli")} | ({"why": x["motivo"]} if x["motivo"] else {})
                         for x in st["providers"]]}


@_strumento()
async def remove_background(layer: str, keep: str = "subject", model: str = "isnet-general-use",
                            backend: str = "local", project: str | None = None) -> dict:
    """将 AI 抠图结果作为图层蒙版，保留原始照片。keep=background 可反相。
    模型：isnet-general-use、u2net、u2net_human_seg（人像）、birefnet-general（效果较好，较慢）。
    backend=stability 使用在线服务代替本地模型。"""
    s = _store(project)
    return await _off(s.remove_background, layer, keep, model, backend)


@_strumento()
async def subject_box(layer: str, project: str | None = None) -> dict:
    """返回图片图层主要主体的边界 [x,y,w,h]，单位为文档像素。"""
    s = _store(project)
    return await _off(s.select_subject_box, layer)


@_strumento()
async def inpaint(box: list | None = None, prompt: str | None = None, ellipse: bool = False,
                  mask_path: str | None = None, feather: float = 6, backend: str = "auto",
                  seed: int | None = None, points: list | None = None, size: float = 30,
                  project: str | None = None) -> dict:
    """移除物体或填充区域：传入文档像素 box=[x,y,w,h]、mask_path，或 points=[[x,y],...]
    加 size 进行笔画式污点修复。不传提示词时使用传统填充；传提示词时通过 ComfyUI 或
    在线服务（backend=openai、gemini、stability 等）生成式填充。结果为带柔边蒙版的新图层，
    可像普通图层一样移动、调透明度或删除。"""
    s = _store(project)
    return await _off(s.inpaint, box, prompt, ellipse, mask_path, feather, backend, seed, None, points, size)


@_strumento()
async def generate_image(prompt: str, width: int | None = None, height: int | None = None,
                         negative: str | None = None, seed: int | None = None, fit: str = "cover",
                         backend: str = "auto", project: str | None = None) -> dict:
    """通过本地 ComfyUI 或在线服务（backend=openai、gemini、stability、openrouter、replicate）
    文生图，结果添加为新图层，fit 可选 cover/contain。"""
    s = _store(project)
    return await _off(s.generate_image, prompt, width, height, negative, seed, fit, None, backend)


@_strumento()
async def edit_image(prompt: str, layer: str | None = None, box: list | None = None, strength: float = 0.6,
                     backend: str = "auto", seed: int | None = None, project: str | None = None) -> dict:
    """图生图：按提示词修改图片图层、选区或整张图片，例如“变成冬天”“水彩风格”。
    strength 为 0..1，低值保留原图，高值增加改动。结果在相同位置和蒙版上方创建新图层，保留原图层。"""
    s = _store(project)
    return await _off(s.edit_image, prompt, layer, box, strength, backend, seed)


@_strumento()
async def describe_image(layer: str | None = None, box: list | None = None, question: str | None = None,
                         backend: str = "auto", project: str | None = None) -> dict:
    """使用视觉模型（在线密钥服务或 Ollama）描述整图、图层或区域，也可回答 question。不会修改文档。"""
    s = _store(project)
    return await _off(s.describe_image, layer, box, question, backend)


@_strumento()
async def upscale(layer: str, factor: float = 2.0, backend: str = "auto", project: str | None = None) -> dict:
    """为图片图层增加实际像素，优先 ComfyUI 模型，backend=stability 可使用在线服务，
    否则使用 Lanczos 插值和锐化。图层在画布上的显示尺寸不变。"""
    s = _store(project)
    return await _off(s.upscale, layer, factor, backend)


@_strumento()
async def auto_enhance(layer: str | None = None, strength: float = 1.0, project: str | None = None) -> dict:
    """测量并修正色阶、白平衡、自然饱和度和对比度，添加普通、可编辑的滤镜，
    默认创建顶层调整图层，也可通过 layer= 添加到指定图层。"""
    s = _store(project)
    return await _off(s.auto_enhance, layer, strength)


@_strumento()
async def match_color(layer: str, reference: str, amount: float = 1.0, project: str | None = None) -> dict:
    """让一个图层的颜色和色调匹配另一个图层，例如让粘贴的主体融入背景。"""
    s = _store(project)
    return await _off(s.match_color, layer, reference, amount)


@_strumento()
def comfy_settings(url: str | None = None, checkpoint: str | None = None, upscaler: str | None = None,
                   steps: int | None = None, cfg: float | None = None) -> dict:
    """连接 ComfyUI 并选择检查点及放大模型；不传参数时返回当前设置。"""
    return comfy.set_config(url=url, checkpoint=checkpoint, upscaler=upscaler, steps=steps, cfg=cfg)


# --------------------------------------------------------------------------
# 导出、批量操作和界面
# --------------------------------------------------------------------------


@_strumento()
async def export_image(path: str, quality: int = 92, scale: float = 1.0, transparent: bool = False,
                       box: list | None = None, project: str | None = None) -> dict:
    """写入图片文件（.png、.jpg、.webp、.avif、.tif），仅在用户要求时执行。
    transparent=true 保留 alpha（PNG/WebP），box 可仅导出指定区域。"""
    s = _store(project)
    api = sys.modules.get("pedit.api")
    if api is not None and api.S.store is s and api.MCP_ATTIVO:
        ok = await anyio.to_thread.run_sync(api.conferma_export, path)
        if not ok:
            return {"exported": False, "reason": "用户未确认导出"}
    return await _off(s.export_image, path, quality, scale, transparent, box)


@_strumento()
async def batch(ops: list[dict], look: bool = False, project: str | None = None) -> list | dict:
    """在一次调用中完成多项编辑，整体成功或回滚，撤销时只需一步。
    格式为 [{"tool": "set_transform", "args": {...}}, ...]。look=true 同时返回预览。"""
    s = _store(project)
    risultati = []
    with s.batch():
        for i, op in enumerate(ops):
            nome = op.get("tool")
            if nome in _FUORI_BATCH or nome not in STRUMENTI:
                raise EditError(f"步骤 {i}: {nome!r} 不能在 batch 中使用")
            fn = STRUMENTI[nome]
            args = dict(op.get("args") or {})
            args.pop("project", None)
            try:
                if inspect.iscoroutinefunction(fn):
                    res = await fn(**args)
                else:
                    res = fn(**args)
            except TypeError as exc:
                raise EditError(f"步骤 {i} ({nome}): {exc}") from exc
            except _PREVISTI as exc:
                raise EditError(f"步骤 {i} ({nome}): {_messaggio(exc)}") from exc
            risultati.append(res)
    out = {"done": len(risultati), "results": risultati}
    if look:
        img = await preview(project=project)
        return [img[0], _json(out)]
    return out


@_strumento()
def open_ui(port: int = 8770, project: str | None = None) -> dict:
    """在当前文档上打开网页编辑器，共享同一内存文档并实时同步。"""
    from . import api

    s = _store(project)
    url = api.serve_background(s, port=port)
    return {"url": url}


@_strumento()
def close_ui() -> dict:
    """停止通过 open_ui 启动的网页编辑器。"""
    from . import api

    return {"stopped": api.stop_background()}


@_strumento()
def report_issue(kind: str, title: str, details: str = "", tool: str = "") -> dict:
    """记录故障、不便、错误、缺失或缓慢的问题（kind: bug、scomodo、sbagliato、manca、lento），然后继续工作。"""
    return segnalazioni.aggiungi(kind, title, details, tool)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()


__all__ = ["mcp", "main", "STRUMENTI", "PRESETS"]
