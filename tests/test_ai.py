"""IA: ripieghi locali veri, ComfyUI simulato (nessuna rete nei test)."""

import importlib.util

import numpy as np
import pytest
from PIL import Image

from pedit import ai, comfy
from pedit.store import Store


def test_diffusion_fill_is_smooth():
    arr = np.zeros((64, 64, 3), np.float32)
    arr[..., 0] = np.linspace(0, 1, 64)[None, :]
    mask = np.zeros((64, 64), bool)
    mask[20:44, 20:44] = True
    hole = arr.copy()
    hole[mask] = [1, 1, 0]
    out = ai._diffusione(hole, mask)
    assert np.abs(out[mask] - arr[mask]).mean() < 0.05


def test_workflows_are_valid_graphs():
    wf = comfy.workflow_inpaint("x.png", "a cat", "bad", 1, "sd.safetensors", 20, 7.0)
    assert wf["4"]["class_type"] == "LoadImage" and wf["5"]["inputs"]["mask"] == ["4", 1]
    wf = comfy.workflow_txt2img("cat", "bad", 512, 512, 1, "sd.safetensors", 20, 7.0)
    assert wf["7"]["class_type"] == "SaveImage"


def test_generate_with_mocked_comfy(monkeypatch, store):
    monkeypatch.setattr(comfy, "available", lambda timeout=1.5: True)
    monkeypatch.setattr(comfy, "checkpoints", lambda: ["sd15.safetensors"])
    visti = {}

    def finto_run(wf, timeout=900):
        visti["wf"] = wf
        return [Image.new("RGBA", (wf["4"]["inputs"]["width"], wf["4"]["inputs"]["height"]), (10, 200, 30, 255))]

    monkeypatch.setattr(comfy, "run", finto_run)
    v = store.generate_image("a green field")
    assert v["type"] == "image"
    assert visti["wf"]["1"]["inputs"]["ckpt_name"] == "sd15.safetensors"
    assert visti["wf"]["2"]["inputs"]["text"] == "a green field"


def test_generative_fill_with_mocked_comfy(monkeypatch, tmp_path, photo):
    s = Store.create(from_image=photo, path=str(tmp_path / "d.json"))
    monkeypatch.setattr(comfy, "available", lambda timeout=1.5: True)
    monkeypatch.setattr(comfy, "checkpoints", lambda: ["sd15-inpainting.safetensors", "other.safetensors"])
    monkeypatch.setattr(comfy, "upload", lambda img, name="x": "up.png")
    monkeypatch.setattr(comfy, "run", lambda wf, timeout=900: [Image.new("RGBA", (64, 64), (0, 0, 255, 255))])
    v = s.inpaint([96, 36, 48, 48], prompt="blue sky")
    assert v["backend"] == "comfyui" and v["name"].startswith("Generative")
    assert comfy.pick_checkpoint(inpaint=True) == "sd15-inpainting.safetensors"


@pytest.mark.ai
@pytest.mark.skipif(importlib.util.find_spec("rembg") is None, reason="extra ai not installed")
def test_remove_background_real(tmp_path):
    arr = np.full((128, 128, 3), 240, np.uint8)
    arr[32:96, 40:88] = [200, 30, 30]
    p = tmp_path / "obj.png"
    Image.fromarray(arr).save(p)
    s = Store.create(from_image=str(p))
    v = s.remove_background(s.project.layers[0].id)
    assert v["mask"]["kind"] == "image"


def test_remove_background_without_extra_explains(monkeypatch, store, photo):
    monkeypatch.setattr(ai, "_ha", lambda m: False)
    a = store.add_image(path=photo)
    with pytest.raises(Exception, match="ai extra"):
        store.remove_background(a["id"])
