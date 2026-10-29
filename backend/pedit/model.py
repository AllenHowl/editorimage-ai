"""文档数据模型。

文档是可完整序列化的纯 JSON，没有隐藏状态。渲染是文档和素材文件的纯函数，
相同文档始终产生相同像素。

编辑为非破坏式：滤镜保存在 ``effects``，蒙版保存在图层字段中，AI 抠图蒙版
也作为独立素材保存，绝不覆盖原图。

坐标使用文档像素，原点在左上角。图层的 ``x``/``y`` 为旋转前内容的左上角，
旋转围绕缩放后内容的中心进行。
"""

from __future__ import annotations

import types
import uuid
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Union, get_args, get_origin, get_type_hints

from PIL import features as _features

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
# Pillow 11.3 起才支持 AVIF，且安装包必须包含 libavif。
if _features.check("avif"):
    IMAGE_EXTS.add(".avif")

LAYER_TYPES = ("image", "text", "shape", "fill", "adjustment", "paint", "group")

# Photoshop 的 27 种混合模式（``add`` 对应线性减淡），另加仅用于组的 ``pass_through``。
# 穿透模式下，子图层直接与组下方内容混合，
# 等同于没有组容器。
BLEND_MODES = (
    "normal", "dissolve",
    "darken", "multiply", "color_burn", "linear_burn", "darker_color",
    "lighten", "screen", "color_dodge", "add", "lighter_color",
    "overlay", "soft_light", "hard_light", "vivid_light", "linear_light", "pin_light", "hard_mix",
    "difference", "exclusion", "subtract", "divide",
    "hue", "saturation", "color", "luminosity",
)
GROUP_BLEND_MODES = ("pass_through",) + BLEND_MODES

SHAPE_KINDS = ("rect", "ellipse", "line", "polygon")
FILL_KINDS = ("solid", "linear", "radial")
MASK_KINDS = ("rect", "ellipse", "image", "paint")


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------
# dataclass 的通用序列化和反序列化。
# --------------------------------------------------------------------------


def to_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        return {f.name: to_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, list):
        return [to_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    return obj


def _unwrap_optional(ftype: Any) -> Any:
    """将 ``TextStyle | None`` 还原为 ``TextStyle``，其他类型保持原样。"""
    if get_origin(ftype) in (Union, types.UnionType):
        args = [a for a in get_args(ftype) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return ftype


def from_dict(cls: type, data: Any) -> Any:
    """从字典构造 dataclass，忽略未知字段。

    向前兼容：未来版本保存的文档仍可打开，仅忽略当前版本不认识的字段。
    """
    if not is_dataclass(cls):
        return data
    if data is None:
        return cls()
    hints = get_type_hints(cls)
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        val = data[f.name]
        ftype = _unwrap_optional(hints[f.name])
        if val is not None and is_dataclass(ftype):
            val = from_dict(ftype, val)
        elif get_origin(ftype) is list and val is not None:
            (inner,) = get_args(ftype) or (Any,)
            if is_dataclass(inner):
                val = [from_dict(inner, v) for v in val]
        kwargs[f.name] = val
    return cls(**kwargs)


# --------------------------------------------------------------------------
# 文档组成部分
# --------------------------------------------------------------------------


@dataclass
class Asset:
    """文档引用的图片文件，始终保持原始内容。

    ``generated`` 标记编辑器生成的文件（AI 蒙版、填充补丁、栅格化图层），
    它们保存在文档素材目录中，可随文档一起删除。
    """
    id: str
    path: str
    width: int = 0
    height: int = 0
    name: str = ""
    generated: bool = False


@dataclass
class Effect:
    type: str
    params: dict = field(default_factory=dict)
    enabled: bool = True


@dataclass
class Mask:
    """图层蒙版：白色显示，黑色隐藏。

    ``rect`` 和 ``ellipse`` 的 ``box`` 使用文档像素，即最终图片中的区域。
    ``image`` 为与图层内容像素对齐的灰度素材，随内容移动、缩放和旋转，
    类似 Photoshop 的链接蒙版；AI 抠图使用此类型。
    ``paint`` 由 ``base`` 指定初始可见性，1 为全部显示，0 为全部隐藏。

    任意形状的蒙版均可叠加文档像素坐标下的 ``strokes``，按颜色亮度
    决定显示程度：白色显示，黑色隐藏，可用于细化 AI 抠图边缘。
    """
    kind: str = "rect"
    box: list[float] | None = None
    asset: str | None = None
    invert: bool = False
    feather: float = 0.0
    density: float = 1.0
    base: float = 1.0
    strokes: list["Stroke"] = field(default_factory=list)


@dataclass
class TextStyle:
    font: str = "sans"
    size: float = 64.0
    color: str = "#ffffff"
    bold: bool = False
    italic: bool = False
    align: str = "left"
    line_spacing: float = 1.2
    stroke_width: float = 0.0
    stroke_color: str = "#000000"
    # 0 表示不自动换行，否则为最大行宽，单位为像素。
    max_width: float = 0.0


@dataclass
class Shape:
    kind: str = "rect"
    w: float = 200.0
    h: float = 200.0
    fill: str | None = "#ffffff"
    stroke: str | None = None
    stroke_width: float = 0.0
    radius: float = 0.0
    # 仅用于 polygon/line：相对图层左上角的顶点坐标。
    points: list[list[float]] | None = None


@dataclass
class Fill:
    kind: str = "solid"
    color: str = "#000000"
    color2: str = "#ffffff"
    angle: float = 90.0
    # 径向渐变：中心以画布宽高比例表示。
    cx: float = 0.5
    cy: float = 0.5


@dataclass
class Stroke:
    """笔画：使用文档像素坐标的折线。

    笔画以矢量形式保存在文档中，渲染时才栅格化，因此删除错误笔画不会影响其他笔画。
    """
    points: list[list[float]] = field(default_factory=list)
    color: str = "#000000"
    size: float = 12.0
    hardness: float = 0.8
    opacity: float = 1.0
    erase: bool = False
    # 仿制图章从当前图层下方合成内容的（笔画点 + clone 偏移）处取样，
    # 启用 heal 时，将光线和颜色匹配到目标区域，
    # 形成修复画笔效果。
    clone: list[float] | None = None
    heal: bool = False


@dataclass
class Layer:
    id: str
    type: str = "image"
    name: str = ""
    visible: bool = True
    locked: bool = False
    opacity: float = 1.0
    blend: str = "normal"
    # 内容变换
    x: float = 0.0
    y: float = 0.0
    scale: float = 1.0
    rotation: float = 0.0
    flip_h: bool = False
    flip_v: bool = False
    # 根据图层类型保存内容。
    asset: str | None = None
    crop: list[float] | None = None
    text: str | None = None
    style: TextStyle | None = None
    shape: Shape | None = None
    fill: Fill | None = None
    strokes: list[Stroke] = field(default_factory=list)
    # 滤镜和样式；调整图层中的滤镜作用于下方内容。
    effects: list[Effect] = field(default_factory=list)
    mask: Mask | None = None
    # 剪贴蒙版：仅在下方图层有内容的区域显示。
    clip: bool = False
    # 仅用于组：子图层从下到上排列。
    children: list["Layer"] = field(default_factory=list)
    collapsed: bool = False


@dataclass
class Project:
    name: str = "未命名"
    width: int = 1920
    height: int = 1080
    background: str = "#ffffff"
    dpi: int = 72
    # 图层从下到上排列，最后一项显示在最上方。
    layers: list[Layer] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    version: int = 1

    def to_dict(self) -> dict:
        return to_dict(self)

    @classmethod
    def from_obj(cls, data: dict) -> "Project":
        return from_dict(cls, data)

    # ---- 查找 ------------------------------------------------------------
    def walk(self, layers: list[Layer] | None = None):
        """遍历所有图层（包括组内图层），从下到上，先组后子图层。"""
        for ly in self.layers if layers is None else layers:
            yield ly
            if ly.children:
                yield from self.walk(ly.children)

    def layer(self, layer_id: str) -> Layer:
        for ly in self.walk():
            if ly.id == layer_id:
                return ly
        # 也支持按名称查找：智能体通常记住“标题”，而非“L3fa9c1d2”。
        for ly in self.walk():
            if ly.name and ly.name == layer_id:
                return ly
        raise KeyError(f"未找到图层：{layer_id}")

    def container(self, layer_id: str) -> list[Layer]:
        """返回包含该图层的列表，即根 ``layers`` 或某个组的子图层。"""
        target = self.layer(layer_id)
        if any(ly is target for ly in self.layers):
            return self.layers
        for ly in self.walk():
            if any(c is target for c in ly.children):
                return ly.children
        raise KeyError(f"未找到图层：{layer_id}")

    def parent(self, layer_id: str) -> Layer | None:
        target = self.layer(layer_id)
        for ly in self.walk():
            if any(c is target for c in ly.children):
                return ly
        return None

    def index_of(self, layer_id: str) -> int:
        """返回图层在所属列表（``container``）中的位置。"""
        target = self.layer(layer_id)
        return next(i for i, ly in enumerate(self.container(layer_id)) if ly is target)

    def asset(self, asset_id: str) -> Asset:
        for a in self.assets:
            if a.id == asset_id:
                return a
        raise KeyError(f"未找到素材：{asset_id}")
