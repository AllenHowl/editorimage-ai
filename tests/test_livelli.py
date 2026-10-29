"""通过像素验证图层组、剪贴蒙版、绘画蒙版、仿制图章和扩图。"""

import json

import numpy as np
import pytest
from conftest import px
from PIL import Image

from pedit import ai, render
from pedit.model import IMAGE_EXTS, Effect, Fill, Layer, Mask, Project, Shape, Stroke
from pedit.store import EditError, Store


def _fondo(color="#646464", w=40, h=30):
    p = Project(width=w, height=h, background="#000000")
    p.layers.append(Layer(id="F", type="fill", fill=Fill(kind="solid", color=color)))
    return p


# --------------------------------------------------------------------------
# 新增混合模式
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode,expect", [
    ("linear_burn", (45, 0, 0)), ("divide", (128, 255, 255)), ("linear_light", (245, 45, 0)),
    ("pin_light", (145, 100, 100)), ("hard_mix", (255, 0, 0)), ("luminosity", (124, 124, 124)),
    ("color", (175, 75, 26)), ("hue", (100, 100, 100)), ("darker_color", (100, 100, 100)),
    ("lighter_color", (200, 100, 50)),
])
def test_new_blend_modes(mode, expect):
    p = _fondo("#646464")
    p.layers.append(Layer(id="T", type="fill", blend=mode, fill=Fill(color="#c86432")))
    got = px(render.render(p), 5, 5)[:3]
    assert all(abs(g - e) <= 2 for g, e in zip(got, expect)), (mode, got)


def test_every_blend_mode_renders():
    from pedit.model import BLEND_MODES

    assert len(BLEND_MODES) == 27
    for mode in BLEND_MODES:
        p = _fondo("#3070b0")
        p.layers.append(Layer(id="T", type="fill", blend=mode, opacity=0.7, fill=Fill(color="#e0a020")))
        arr = np.asarray(render.render(p))
        assert np.isfinite(arr).all(), mode


def test_dissolve_is_all_or_nothing_and_stable():
    p = _fondo("#000000", 64, 64)
    p.layers.append(Layer(id="T", type="fill", blend="dissolve", opacity=0.5, fill=Fill(color="#ffffff")))
    a = np.asarray(render.render(p).convert("L"))
    assert set(np.unique(a)) <= {0, 255}
    assert 0.3 < (a == 255).mean() < 0.7
    assert (a == np.asarray(render.render(p).convert("L"))).all()


# --------------------------------------------------------------------------
# 图层组
# --------------------------------------------------------------------------


def _invert():
    return Layer(id="A", type="adjustment", effects=[Effect(type="invert", params={})])


def test_pass_through_group_affects_below():
    p = _fondo("#ff0000")
    p.layers.append(Layer(id="G", type="group", blend="pass_through", children=[_invert()]))
    assert px(render.render(p), 5, 5)[:3] == (0, 255, 255)


def test_isolated_group_keeps_adjustment_inside():
    p = _fondo("#ff0000")
    p.layers.append(Layer(id="G", type="group", blend="normal", children=[_invert()]))
    assert px(render.render(p), 5, 5)[:3] == (255, 0, 0)


def test_group_opacity_applies_to_the_whole_group():
    p = _fondo("#000000")
    p.layers.append(Layer(id="G", type="group", blend="normal", opacity=0.5, children=[
        Layer(id="W", type="fill", fill=Fill(color="#ffffff")),
        Layer(id="W2", type="fill", fill=Fill(color="#ffffff"))]))
    assert abs(px(render.render(p), 5, 5)[0] - 128) <= 2


def test_hidden_group_hides_children():
    p = _fondo("#000000")
    p.layers.append(Layer(id="G", type="group", visible=False, children=[Layer(id="W", type="fill", fill=Fill(color="#ffffff"))]))
    assert px(render.render(p), 5, 5)[0] == 0


# --------------------------------------------------------------------------
# 剪贴蒙版
# --------------------------------------------------------------------------


def test_clip_shows_only_inside_base():
    p = _fondo("#000000")
    p.layers.append(Layer(id="B", type="shape", x=10, y=10, shape=Shape(w=10, h=10, fill="#ffffff")))
    p.layers.append(Layer(id="C", type="fill", clip=True, fill=Fill(color="#ff0000")))
    img = render.render(p)
    assert px(img, 15, 15)[:3] == (255, 0, 0)
    assert px(img, 3, 3)[:3] == (0, 0, 0)


def test_clipped_adjustment_only_changes_the_base():
    p = _fondo("#000000")
    p.layers.append(Layer(id="B", type="shape", x=10, y=10, shape=Shape(w=10, h=10, fill="#0000ff")))
    adj = _invert()
    adj.clip = True
    p.layers.append(adj)
    img = render.render(p)
    assert px(img, 15, 15)[:3] == (255, 255, 0)
    assert px(img, 3, 3)[:3] == (0, 0, 0)


def test_clip_series_uses_base_blend_and_opacity():
    p = _fondo("#000000")
    p.layers.append(Layer(id="B", type="fill", opacity=0.5, fill=Fill(color="#ffffff")))
    p.layers.append(Layer(id="C", type="fill", clip=True, fill=Fill(color="#ff0000")))
    assert abs(px(render.render(p), 5, 5)[0] - 128) <= 2


# --------------------------------------------------------------------------
# 绘画蒙版
# --------------------------------------------------------------------------


def _bianco(**kw):
    p = _fondo("#000000")
    p.layers.append(Layer(id="W", type="fill", fill=Fill(color="#ffffff"), **kw))
    return p


def test_paint_mask_hides_where_painted():
    st = Stroke(points=[[0, 5], [39, 5]], color="#000000", size=6, hardness=1)
    img = render.render(_bianco(mask=Mask(kind="paint", strokes=[st])))
    assert px(img, 15, 5)[0] < 5
    assert px(img, 15, 20)[0] > 250


def test_paint_mask_hide_all_then_reveal():
    st = Stroke(points=[[0, 5], [39, 5]], color="#ffffff", size=6, hardness=1)
    img = render.render(_bianco(mask=Mask(kind="paint", base=0.0, strokes=[st])))
    assert px(img, 15, 5)[0] > 250
    assert px(img, 15, 20)[0] < 5


def test_strokes_refine_any_mask():
    st = Stroke(points=[[30, 25], [35, 25]], color="#ffffff", size=4, hardness=1)
    img = render.render(_bianco(mask=Mask(kind="rect", box=[0, 0, 10, 10], strokes=[st])))
    assert px(img, 5, 5)[0] > 250 and px(img, 32, 25)[0] > 250 and px(img, 20, 15)[0] < 5


# --------------------------------------------------------------------------
# 仿制图章和修复画笔
# --------------------------------------------------------------------------


def _due_meta(sx="#ff0000", dx="#0000ff"):
    p = Project(width=60, height=40, background="#000000")
    p.layers.append(Layer(id="L", type="shape", x=0, y=0, shape=Shape(w=30, h=40, fill=sx)))
    p.layers.append(Layer(id="R", type="shape", x=30, y=0, shape=Shape(w=30, h=40, fill=dx)))
    return p


def test_clone_copies_from_below():
    p = _due_meta()
    p.layers.append(Layer(id="P", type="paint", strokes=[
        Stroke(points=[[45, 20]], size=10, hardness=1, clone=[-30, 0])]))
    assert px(render.render(p), 45, 20)[:3] == (255, 0, 0)
    # 单独预览图层时，仿制图章仍可读取下方合成内容。
    assert px(render.layer_image(p, "P"), 45, 20)[:3] == (255, 0, 0)


def test_heal_takes_light_from_destination():
    p = _due_meta("#323232", "#c8c8c8")
    p.layers.append(Layer(id="P", type="paint", strokes=[
        Stroke(points=[[45, 20]], size=24, hardness=1, clone=[-30, 0], heal=True)]))
    assert px(render.render(p), 45, 20)[0] > 150
    p.layers[-1].strokes[0].heal = False
    assert px(render.render(p), 45, 20)[0] < 60


# --------------------------------------------------------------------------
# Store 操作
# --------------------------------------------------------------------------


def _due_livelli(store):
    a = store.add_shape(x=10, y=10, w=50, h=50, fill="#ff0000", name="A")
    b = store.add_shape(x=40, y=40, w=50, h=50, fill="#00ff00", name="B")
    return a["id"], b["id"]


def test_group_ungroup_keeps_the_picture(store):
    a, b = _due_livelli(store)
    prima = np.asarray(render.render(store.project, store.base_dir))
    g = store.group_layers([a, b], name="Pair")
    assert g["type"] == "group" and [c["id"] for c in g["children"]] == [b, a]
    assert len(store.project.layers) == 1
    assert (np.asarray(render.render(store.project, store.base_dir)) == prima).all()
    store.ungroup(g["id"])
    assert [ly.id for ly in store.project.layers] == [a, b]
    store.undo()
    assert store.project.layers[0].type == "group"


def test_move_into_and_out_of_group(store):
    a, b = _due_livelli(store)
    g = store.add_group(name="G")
    store.move_layer(a, into=g["id"])
    assert store.project.parent(a).id == g["id"]
    store.move_layer(a, into="root")
    assert store.project.parent(a) is None
    with pytest.raises(EditError, match="移入自身"):
        store.move_layer(g["id"], into=g["id"])


def test_nested_layers_are_editable_and_saved(store, tmp_path):
    a, b = _due_livelli(store)
    g = store.group_layers([a, b])
    store.set_layer(a, opacity=0.5)
    store.set_transform(b, x=0)
    copia = store.duplicate_layer(g["id"])
    ids = [ly.id for ly in store.project.walk()]
    assert len(ids) == len(set(ids)) == 6
    store.remove_layer(a)
    s2 = Store.open(store.path)
    assert [ly.type for ly in s2.project.walk()] == ["group", "shape", "group", "shape", "shape"]
    assert s2.project.layer(copia["id"]).children[0].opacity == 0.5


def test_clip_needs_a_layer_below(store):
    a, b = _due_livelli(store)
    with pytest.raises(EditError, match="下方图层"):
        store.set_layer(a, clip=True)
    assert store.set_layer(b, clip=True)["clip"] is True


def test_group_blend_modes(store):
    a, b = _due_livelli(store)
    g = store.group_layers([a])
    store.set_layer(g["id"], blend="normal")
    with pytest.raises(EditError):
        store.set_layer(b, blend="pass_through")


def test_mask_stroke_creates_and_paints(store):
    f = store.add_fill(color="#ffffff")
    v = store.mask_stroke(f["id"], [[0, 100], [400, 100]], mode="hide", size=20, hardness=1)
    assert v["mask"]["kind"] == "paint" and v["mask"]["strokes"] == 1
    img = render.render(store.project, store.base_dir)
    assert px(img, 200, 100)[0] < 5 and px(img, 200, 20)[0] > 250
    store.update_mask(f["id"], invert=True)
    store.mask_stroke(f["id"], [[0, 20], [400, 20]], mode="hide", size=10, hardness=1)
    assert px(render.render(store.project, store.base_dir), 200, 20)[0] < 5
    store.remove_mask_stroke(f["id"])
    assert len(store.project.layer(f["id"]).mask.strokes) == 1


def test_clone_stroke_from_store(store):
    store.add_shape(x=0, y=0, w=200, h=200, fill="#ff0000")
    store.add_shape(x=200, y=0, w=200, h=200, fill="#0000ff")
    r = store.paint_stroke([[300, 100]], size=20, hardness=1, source=[100, 100])
    assert store.project.layer(r["layer"]).strokes[0].clone == [-200.0, 0.0]
    assert px(render.render(store.project, store.base_dir), 300, 100)[:3] == (255, 0, 0)
    with pytest.raises(EditError, match="source"):
        store.paint_stroke([[10, 10]], heal=True)


def test_crop_and_resize_move_mask_strokes(store):
    f = store.add_fill(color="#ffffff")
    store.mask_stroke(f["id"], [[100, 100]], size=10)
    store.crop_canvas(50, 50, 200, 100)
    assert store.project.layer(f["id"]).mask.strokes[0].points == [[50.0, 50.0]]
    store.resize_image(width=400)
    st = store.project.layer(f["id"]).mask.strokes[0]
    assert st.points == [[100.0, 100.0]] and st.size == 20


def test_spot_heal_with_points(store):
    store.add_fill(color="#808080")
    store.add_shape(x=190, y=90, w=20, h=20, fill="#ff0000")
    v = store.inpaint(points=[[200, 100]], size=40, backend="diffusion")
    assert v["name"].startswith("修复结果")
    r, g, b, _ = px(render.render(store.project, store.base_dir), 200, 100)
    assert abs(r - g) < 40


def test_expand_canvas_classic(store, photo):
    a = store.add_image(path=photo, x=0, y=0, fit="none")
    v = store.expand_canvas(left=40, right=40, backend="diffusion")
    assert (v["width"], v["height"]) == (480, 200)
    assert store.project.layers[-1].name.startswith("扩图结果")
    assert store.project.layer(a["id"]).x == 40
    img = render.render(store.project, store.base_dir)
    assert px(img, 5, 50)[3] == 255
    store.undo()
    assert store.project.width == 400 and store.project.layer(a["id"]).x == 0


def test_expand_failure_leaves_document_intact(store, monkeypatch):
    store.add_fill(color="#808080")

    def rotto(*a, **k):
        raise ai.AIError("no service")

    monkeypatch.setattr(ai, "fill", rotto)
    with pytest.raises(EditError, match="no service"):
        store.expand_canvas(top=20, prompt="sky")
    assert store.project.height == 200 and len(store.project.layers) == 1


def test_edit_image_layer_goes_above(store, photo, monkeypatch):
    a = store.add_image(path=photo, x=30, y=20)
    store.set_mask(a["id"], kind="ellipse", box=[30, 20, 100, 100])
    visti = {}

    def finto(img, prompt, strength, seed, backend="auto"):
        visti.update(size=img.size, prompt=prompt, strength=strength, backend=backend)
        return Image.new("RGB", img.size, (0, 255, 0)), "openai"

    monkeypatch.setattr(ai, "edit", finto)
    v = store.edit_image("make it green", layer=a["id"], strength=0.4, backend="openai")
    assert visti == {"size": (240, 120), "prompt": "make it green", "strength": 0.4, "backend": "openai"}
    assert v["backend"] == "openai" and v["box"][:2] == [30.0, 20.0] and v["mask"]["kind"] == "ellipse"
    assert store.project.layers[-1].id == v["id"]


def test_describe_image_is_read_only(store, monkeypatch):
    store.add_fill(color="#ff0000")
    monkeypatch.setattr(ai, "describe", lambda img, q, backend="auto": (f"red {img.size}", "gemini"))
    v0 = store.version
    out = store.describe_image(box=[0, 0, 100, 50])
    assert out == {"text": "red (100, 50)", "backend": "gemini"} and store.version == v0


@pytest.mark.skipif(".avif" not in IMAGE_EXTS, reason="Pillow without AVIF")
def test_export_avif(store, tmp_path):
    store.add_fill(color="#3366cc")
    r = store.export_image(str(tmp_path / "out.avif"), quality=60)
    with Image.open(r["path"]) as im:
        assert im.format == "AVIF" and im.size == (400, 200)


def test_layer_view_json_roundtrip(store):
    a, b = _due_livelli(store)
    store.group_layers([a, b])
    json.dumps(store.summary("full"))
