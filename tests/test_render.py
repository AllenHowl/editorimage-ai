"""检查渲染像素，不仅要求无异常，还需验证实际效果符合预期。"""

import numpy as np
import pytest
from conftest import px

from pedit import effects as fx
from pedit import render
from pedit.model import Effect, Fill, Layer, Mask, Project, Shape, Stroke, TextStyle


def _solid(color="#808080", w=40, h=30, **kw):
    p = Project(width=w, height=h, background="#000000")
    p.layers.append(Layer(id="F", type="fill", fill=Fill(kind="solid", color=color), **kw))
    return p


@pytest.mark.parametrize("name", [n for n in fx.REGISTRY if n != "color_match"])
def test_every_effect_renders(name):
    p = _solid("#7f6050")
    p.layers.append(Layer(id="S", type="shape", x=5, y=5, shape=Shape(w=20, h=15, fill="#20a0ff"),
                          effects=[Effect(type=name, params=fx.validate(name, {}))]))
    assert render.render(p).size == (40, 30)


def _mean(img):
    return np.asarray(img.convert("RGB"), np.float32).reshape(-1, 3).mean(0)


@pytest.mark.parametrize("name,params,check", [
    ("brightness", {"amount": 0.4}, lambda a, b: b.mean() > a.mean() + 20),
    ("brightness", {"amount": -0.4}, lambda a, b: b.mean() < a.mean() - 20),
    ("exposure", {"stops": 1}, lambda a, b: b.mean() > a.mean() * 1.4),
    ("temperature", {"amount": 0.8}, lambda a, b: b[0] > a[0] and b[2] < a[2]),
    ("temperature", {"amount": -0.8}, lambda a, b: b[0] < a[0] and b[2] > a[2]),
    ("tint", {"amount": 0.8}, lambda a, b: b[1] < a[1]),
    ("saturation", {"amount": -1}, lambda a, b: abs(b[0] - b[1]) < 2 and abs(b[1] - b[2]) < 2),
    ("saturation", {"amount": 0.8}, lambda a, b: (b.max() - b.min()) > (a.max() - a.min())),
    ("invert", {}, lambda a, b: abs(b[0] - (255 - a[0])) < 2),
    ("grayscale", {}, lambda a, b: abs(b[0] - b[2]) < 2),
    ("contrast", {"amount": 0.5}, lambda a, b: b[0] > a[0] and b[2] < a[2]),
])
def test_effect_direction(name, params, check):
    p = _solid("#b07050")
    before = _mean(render.render(p))
    p.layers[0].effects.append(Effect(type=name, params=fx.validate(name, params)))
    after = _mean(render.render(p))
    assert check(before, after), (before, after)


def test_validate_rejects_out_of_range():
    with pytest.raises(fx.EffectError):
        fx.validate("blur", {"radius": -1})
    with pytest.raises(fx.EffectError):
        fx.validate("blur", {"nope": 1})


@pytest.mark.parametrize("mode,expect", [
    ("normal", (200, 100, 50)), ("multiply", (78, 39, 20)), ("screen", (222, 161, 132)),
    ("darken", (100, 100, 50)), ("lighten", (200, 100, 100)), ("difference", (100, 0, 50)),
])
def test_blend_modes(mode, expect):
    p = _solid("#646464")
    p.layers.append(Layer(id="T", type="fill", blend=mode, fill=Fill(color="#c86432")))
    got = px(render.render(p), 5, 5)[:3]
    assert all(abs(g - e) <= 2 for g, e in zip(got, expect)), (mode, got)


def test_opacity_half():
    p = _solid("#000000")
    p.layers.append(Layer(id="T", type="fill", opacity=0.5, fill=Fill(color="#ffffff")))
    assert abs(px(render.render(p), 1, 1)[0] - 128) <= 2


def test_rect_mask_hides_outside():
    p = _solid("#000000")
    p.layers.append(Layer(id="T", type="fill", fill=Fill(color="#ffffff"), mask=Mask(kind="rect", box=[10, 10, 10, 10])))
    img = render.render(p)
    assert px(img, 15, 15)[0] > 250
    assert px(img, 2, 2)[0] < 5


def test_inverted_mask():
    p = _solid("#000000")
    p.layers.append(Layer(id="T", type="fill", fill=Fill(color="#ffffff"),
                          mask=Mask(kind="rect", box=[10, 10, 10, 10], invert=True)))
    img = render.render(p)
    assert px(img, 15, 15)[0] < 5 and px(img, 2, 2)[0] > 250


def test_adjustment_affects_below_only():
    p = _solid("#808080")
    p.layers.append(Layer(id="A", type="adjustment", effects=[Effect(type="invert", params={})]))
    p.layers.append(Layer(id="S", type="shape", x=0, y=0, shape=Shape(w=10, h=10, fill="#ff0000")))
    img = render.render(p)
    assert px(img, 30, 20)[0] == pytest.approx(127, abs=2)    # 灰色反相。
    assert px(img, 4, 4)[:3] == (255, 0, 0)                   # 调整图层上方的内容保持不变。


def test_shape_lands_at_xy():
    p = Project(width=100, height=100, background="#000000")
    p.layers.append(Layer(id="S", type="shape", x=20, y=30, shape=Shape(w=40, h=20, fill="#ffffff")))
    img = render.render(p)
    assert px(img, 21, 31)[0] > 200 and px(img, 58, 48)[0] > 200
    assert px(img, 18, 31)[0] < 30 and px(img, 21, 28)[0] < 30


def test_preview_matches_full_render():
    p = Project(width=200, height=100, background="#203040")
    p.layers.append(Layer(id="S", type="shape", x=40, y=20, shape=Shape(w=100, h=60, fill="#ff8800"),
                          effects=[Effect(type="drop_shadow", params=fx.validate("drop_shadow", {"dx": 10, "dy": 10}))]))
    full = render.render(p).resize((100, 50))
    half = render.render(p, scale=0.5)
    diff = np.abs(np.asarray(full, np.float32) - np.asarray(half, np.float32)).mean()
    assert diff < 6


def test_gradient_direction():
    p = Project(width=100, height=10, background="#000000")
    p.layers.append(Layer(id="G", type="fill", fill=Fill(kind="linear", color="#000000", color2="#ffffff", angle=90)))
    img = render.render(p)
    assert px(img, 2, 5)[0] < 20 and px(img, 97, 5)[0] > 235


def test_paint_and_erase():
    p = Project(width=60, height=60, background="#000000")
    ly = Layer(id="P", type="paint", strokes=[Stroke(points=[[5, 30], [55, 30]], color="#ffffff", size=10, hardness=1)])
    p.layers.append(ly)
    assert px(render.render(p), 30, 30)[0] > 240
    ly.strokes.append(Stroke(points=[[30, 20], [30, 40]], size=12, hardness=1, erase=True))
    img = render.render(p)
    assert px(img, 30, 30)[0] < 10 and px(img, 10, 30)[0] > 240


def test_text_renders_pixels():
    p = Project(width=300, height=100, background="#000000")
    p.layers.append(Layer(id="T", type="text", x=10, y=10, text="Hello", style=TextStyle(size=40, color="#ffffff")))
    arr = np.asarray(render.render(p).convert("L"))
    assert (arr > 200).sum() > 100


def test_rotation_keeps_center():
    p = Project(width=100, height=100, background="#000000")
    p.layers.append(Layer(id="S", type="shape", x=30, y=45, rotation=90, shape=Shape(w=40, h=10, fill="#ffffff")))
    img = render.render(p)
    # 围绕中心（50,50）旋转 90 度后变为竖直方向。
    assert px(img, 50, 32)[0] > 200 and px(img, 35, 50)[0] < 30


def test_save_formats(tmp_path):
    img = render.render(_solid("#ff0000"))
    for ext in (".png", ".jpg", ".webp", ".tif"):
        assert render.save_image(img, tmp_path / f"o{ext}").stat().st_size > 0
    with pytest.raises(ValueError):
        render.save_image(img, tmp_path / "o.xyz")
