"""Operazioni di Store: modifiche, undo, atomicita', non distruttivita'."""

import json
from pathlib import Path

import numpy as np
import pytest
from conftest import px
from PIL import Image

from pedit import render
from pedit.store import EditError, Store


def test_create_presets_and_from_image(tmp_path, photo):
    s = Store.create(preset="square")
    assert (s.project.width, s.project.height) == (1080, 1080)
    s2 = Store.create(from_image=photo)
    assert (s2.project.width, s2.project.height) == (240, 120)
    assert s2.project.layers[0].type == "image" and s2._undo == []
    with pytest.raises(EditError):
        Store.create(preset="nope")


def test_open_photo_makes_document(photo):
    s = Store.open(photo)
    assert s.path is None and len(s.project.layers) == 1


def test_autosave_and_reopen(store, photo):
    ly = store.add_image(path=photo)
    data = json.loads(Path(store.path).read_text(encoding="utf-8"))
    assert data["layers"][0]["id"] == ly["id"]
    again = Store.open(store.path)
    assert again.project.layers[0].asset == ly["asset"]


def test_relative_asset_paths_survive_move(tmp_path, photo):
    d = tmp_path / "proj"
    d.mkdir()
    img = d / "p.png"
    img.write_bytes(Path(photo).read_bytes())
    s = Store.create(path=str(d / "doc.json"), width=100, height=100)
    s.add_image(path=str(img))
    assert s.project.assets[0].path == "p.png"


def test_add_image_fit(store, photo):
    v = store.add_image(path=photo, fit="cover")
    assert v["box"][2] >= 400 - 0.5 and v["box"][3] >= 200 - 0.5
    v = store.add_image(path=photo, fit="contain")
    assert v["box"][2] <= 400.5 and v["box"][3] <= 200.5


def test_undo_redo(store):
    a = store.add_text("Hi")
    store.set_text(a["id"], size=10)
    store.undo()
    assert store.project.layer(a["id"]).style.size == 64
    store.redo()
    assert store.project.layer(a["id"]).style.size == 10
    store.undo()
    store.undo()
    assert not store.project.layers
    with pytest.raises(EditError):
        store.undo()


def test_failed_edit_leaves_document_unchanged(store):
    a = store.add_text("Hi")
    n = len(store._undo)
    with pytest.raises(Exception):
        store.set_text(a["id"], align="sideways")
    assert len(store._undo) == n
    assert store.project.layer(a["id"]).style.align == "left"


def test_batch_is_atomic(store):
    with pytest.raises(EditError):
        with store.batch():
            store.add_text("one")
            store.add_shape(kind="star")
    assert not store.project.layers
    with store.batch():
        store.add_text("one")
        store.add_text("two")
    assert len(store.project.layers) == 2
    store.undo()
    assert not store.project.layers


def test_locked_layer(store):
    a = store.add_text("Hi")
    store.set_layer(a["id"], locked=True)
    with pytest.raises(EditError, match="locked"):
        store.set_transform(a["id"], x=5)
    store.set_layer(a["id"], locked=False)
    store.set_transform(a["id"], x=5)


def test_errors_are_explanatory(store):
    with pytest.raises(EditError, match="layers"):
        store.set_layer("nope", opacity=0.5)
    a = store.add_shape()
    with pytest.raises(EditError, match="blend"):
        store.set_layer(a["id"], blend="weird")


def test_align_layer(store):
    a = store.add_shape(w=100, h=50)
    v = store.align_layer(a["id"], "right", "bottom", margin=10)
    assert v["box"][:2] == [290.0, 140.0]
    v = store.align_layer(a["id"], "center", "middle")
    assert v["box"][:2] == [150.0, 75.0]


def test_move_layer_order(store):
    a = store.add_shape(name="A")
    b = store.add_shape(name="B")
    store.move_layer(a["id"], to="top")
    assert store.project.layers[-1].id == a["id"]
    store.move_layer(a["id"], index=0)
    assert store.project.layers[0].id == a["id"] and store.project.layers[1].id == b["id"]


def test_effects_crud(store):
    a = store.add_shape()
    store.add_effect("blur", a["id"], {"radius": 3})
    store.add_effect("drop_shadow", a["id"])
    store.update_effect(a["id"], 0, {"radius": 5}, enabled=False)
    ly = store.project.layer(a["id"])
    assert ly.effects[0].params["radius"] == 5 and not ly.effects[0].enabled
    store.move_effect(a["id"], 1, 0)
    assert ly.effects[0].type == "drop_shadow"
    store.remove_effect(a["id"], 0)
    assert [e.type for e in ly.effects] == ["blur"]


def test_adjustment_rejects_styles(store):
    with pytest.raises(EditError):
        store.add_adjustment("drop_shadow")


def test_look_as_adjustment_and_on_layer(store):
    v = store.apply_look("cinematic")
    assert v["type"] == "adjustment"
    a = store.add_shape()
    store.apply_look("warm", a["id"])
    assert [e.type for e in store.project.layer(a["id"]).effects] == ["temperature", "vibrance"]


def test_crop_canvas_keeps_layers_whole(store):
    a = store.add_shape(x=100, y=50, w=40, h=40)
    store.crop_canvas(80, 40, 100, 80)
    ly = store.project.layer(a["id"])
    assert (store.project.width, store.project.height) == (100, 80)
    assert (ly.x, ly.y) == (20, 10)
    store.undo()
    assert store.project.width == 400


def test_resize_image_scales_everything(store):
    a = store.add_shape(x=100, y=50, w=40, h=40)
    store.add_effect("blur", a["id"], {"radius": 4})
    store.resize_image(width=200)
    ly = store.project.layer(a["id"])
    assert store.project.height == 100
    assert ly.x == 50 and ly.scale == 0.5 and ly.effects[0].params["radius"] == 2


def test_resize_canvas_anchor(store):
    a = store.add_shape(x=0, y=0)
    store.resize_canvas(600, 200, anchor="right")
    assert store.project.layer(a["id"]).x == 200


def test_rasterize_and_merge(store):
    t = store.add_text("Hi", x=10, y=10, size=40)
    v = store.rasterize_layer(t["id"])
    assert v["type"] == "image" and Path(store.project.asset(v["asset"]).path).suffix == ".png"
    s = store.add_shape(x=0, y=0, w=50, h=50, fill="#ff0000")
    m = store.merge_down(s["id"])
    assert len(store.project.layers) == 1 and m["type"] == "image"


def test_flatten(store, photo):
    store.add_image(path=photo)
    store.add_text("x")
    store.flatten()
    assert len(store.project.layers) == 1


def test_mask_from_box_and_update(store):
    a = store.add_fill(color="#ffffff")
    store.set_mask(a["id"], "rect", [0, 0, 100, 100])
    img = render.render(store.project, store.base_dir)
    assert px(img, 50, 50)[0] > 250 and px(img, 300, 150)[0] < 5
    store.update_mask(a["id"], invert=True)
    img = render.render(store.project, store.base_dir)
    assert px(img, 50, 50)[0] < 5


def test_inpaint_removes_square(tmp_path, photo):
    s = Store.create(from_image=photo, path=str(tmp_path / "d.json"))
    before = render.render(s.project, s.base_dir)
    assert px(before, 120, 60)[:3] == (250, 230, 20)
    v = s.inpaint([96, 36, 48, 48], backend="diffusion")
    assert v["backend"] == "diffusion"
    after = render.render(s.project, s.base_dir)
    r, g, b, _ = px(after, 120, 60)
    assert g < 150 and b > 60          # niente piu' giallo: riempito dal gradiente attorno
    # non distruttivo: l'immagine originale e' intatta, la toppa e' un livello in piu'
    assert len(s.project.layers) == 2
    s.remove_layer(v["id"])
    assert px(render.render(s.project, s.base_dir), 120, 60)[:3] == (250, 230, 20)


def test_generative_without_comfy_explains(store):
    with pytest.raises(EditError, match="ComfyUI"):
        store.generate_image("a cat")
    with pytest.raises(EditError, match="ComfyUI"):
        store.inpaint([10, 10, 20, 20], prompt="a cat")


def test_upscale_lanczos_keeps_canvas_size(store, photo):
    a = store.add_image(path=photo, fit="none", x=0, y=0)
    v = store.upscale(a["id"], 2)
    assert v["backend"] == "lanczos" and v["pixels"] == [480, 240]
    assert v["box"][2] == pytest.approx(240) and v["box"][3] == pytest.approx(120)


def test_auto_enhance_fixes_dull_image(tmp_path):
    arr = np.full((60, 80, 3), 0, np.uint8)
    arr[..., 0] = np.linspace(90, 150, 80)[None, :]
    arr[..., 1] = 110
    arr[..., 2] = 80
    p = tmp_path / "dull.png"
    Image.fromarray(arr).save(p)
    s = Store.create(from_image=str(p))
    v = s.auto_enhance()
    types = [e["type"] for e in v["applied"]]
    assert "levels" in types
    out = np.asarray(render.render(s.project).convert("L"), np.float32)
    assert out.std() > np.asarray(Image.open(p).convert("L"), np.float32).std()


def test_match_color_moves_toward_reference(store):
    a = store.add_shape(x=0, y=0, w=100, h=100, fill="#3050c0")
    b = store.add_shape(x=200, y=0, w=100, h=100, fill="#e07030")
    store.match_color(a["id"], b["id"])
    img = render.render(store.project, store.base_dir)
    r, g, bb, _ = px(img, 50, 50)
    assert r > bb


def test_export(store, tmp_path):
    store.add_fill(color="#ff0000")
    out = store.export_image(str(tmp_path / "x.jpg"), quality=80, scale=0.5)
    assert out["width"] == 200 and Image.open(out["path"]).size == (200, 100)
    with pytest.raises(EditError):
        store.export_image(str(tmp_path / "x.xyz"))


def test_paint_stroke_creates_layer(store):
    v = store.paint_stroke([[10, 10], [100, 10]], color="#ffffff", size=8)
    v2 = store.paint_stroke([[10, 50], [100, 50]])
    assert v["layer"] == v2["layer"] and v2["strokes"] == 2
    store.remove_stroke(v["layer"])
    assert len(store.project.layer(v["layer"]).strokes) == 1
