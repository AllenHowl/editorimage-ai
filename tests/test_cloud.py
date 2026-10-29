"""使用用户密钥的在线服务：验证请求格式、响应解析和清晰的错误提示。

不访问网络，以记录请求并模拟真实响应的服务替换 ``cloud._http``。
"""

import base64
import io
import json

import pytest
from PIL import Image

from pedit import ai, cloud, llm
from pedit.store import EditError


def _png(color=(0, 200, 0), size=(64, 48)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _b64(color=(0, 200, 0), size=(64, 48)):
    return base64.b64encode(_png(color, size)).decode()


class Finto:
    """按顺序返回给定响应，并记录请求。"""

    def __init__(self, *risposte):
        self.risposte = list(risposte)
        self.richieste = []

    def __call__(self, method, url, headers, body=None, timeout=300.0):
        self.richieste.append({"method": method, "url": url, "headers": headers, "body": body})
        stato, corpo = self.risposte.pop(0)
        if isinstance(corpo, (dict, list)):
            corpo = json.dumps(corpo).encode()
        return stato, {}, corpo

    def json(self, i=0):
        return json.loads(self.richieste[i]["body"])


@pytest.fixture
def finto(monkeypatch):
    def fai(*risposte):
        f = Finto(*risposte)
        monkeypatch.setattr(cloud, "_http", f)
        return f
    return fai


@pytest.fixture(autouse=True)
def _senza_chiavi(monkeypatch):
    for p in cloud.PROVIDERS:
        if p.get("env"):
            monkeypatch.delenv(p["env"], raising=False)


def test_status_never_shows_the_key():
    cloud.imposta("openai", chiave="sk-abcdefghijklmnop")
    st = cloud.stato()
    o = next(p for p in st["providers"] if p["id"] == "openai")
    assert o["pronto"] and "abcdefghijkl" not in json.dumps(st)
    assert o["chiave"].startswith("sk-a")


def test_key_from_the_assistant_settings_is_reused():
    cfg = llm.carica()
    cfg["chiavi"]["openai"] = "sk-from-chat"
    llm.salva(cfg)
    assert cloud.riassunto()["ready"]["openai"]


def test_auto_never_spends_without_a_default():
    cloud.imposta("openai", chiave="sk-x")
    assert cloud.scegli("generate") is None
    assert cloud.scegli("describe") == "openai"      # 没有内置本地图片描述引擎。
    cloud.imposta("openai", default=True)
    assert cloud.scegli("generate") == "openai"
    assert cloud.scegli("upscale") is None            # OpenAI 不支持此任务，继续使用本地实现。


def test_named_service_without_key_explains():
    with pytest.raises(cloud.CloudError, match="请粘贴 Stability AI API 密钥"):
        cloud.scegli("generate", "stability")
    with pytest.raises(cloud.CloudError, match="不支持 upscale"):
        cloud.scegli("upscale", "gemini")


def test_models_are_per_task_and_validated():
    cloud.imposta("replicate", modelli={"generate": "owner/model-x"})
    assert cloud.modello(cloud.provider("replicate"), "generate") == "owner/model-x"
    assert cloud.modello(cloud.provider("replicate"), "inpaint") == "black-forest-labs/flux-fill-pro"
    with pytest.raises(cloud.CloudError):
        cloud.imposta("anthropic", modelli={"generate": "x"})


def test_openai_generate(finto):
    cloud.imposta("openai", chiave="sk-test")
    f = finto((200, {"data": [{"b64_json": _b64()}]}))
    img = cloud.generate("openai", "a cat", 1600, 900)
    r = f.richieste[0]
    assert r["url"] == "https://api.openai.com/v1/images/generations"
    assert r["headers"]["Authorization"] == "Bearer sk-test"
    assert f.json() == {"model": "gpt-image-1", "prompt": "a cat", "n": 1, "size": "1536x1024"}
    assert img.size == (64, 48)


def test_openai_inpaint_sends_transparent_mask(finto):
    cloud.imposta("openai", chiave="sk-test")
    f = finto((200, {"data": [{"b64_json": _b64(size=(1024, 1024))}]}))
    src = Image.new("RGB", (200, 100), (255, 0, 0))
    m = Image.new("L", (200, 100), 0)
    m.paste(255, (50, 25, 150, 75))
    out = cloud.inpaint("openai", src, m, "a bench")
    r = f.richieste[0]
    assert r["url"].endswith("/images/edits") and "multipart/form-data" in r["headers"]["Content-Type"]
    assert b'name="mask"' in r["body"] and b'name="image"' in r["body"] and b"a bench" in r["body"]
    assert out.size == (200, 100)


def test_gemini_generate_and_describe(finto):
    cloud.imposta("gemini", chiave="g-key")
    f = finto((200, {"candidates": [{"content": {"parts": [{"text": "here"}, {"inlineData": {"mimeType": "image/png", "data": _b64()}}]}}]}),
              (200, {"candidates": [{"content": {"parts": [{"text": "A red square."}]}}]}))
    img = cloud.generate("gemini", "a fox", 1080, 1920)
    assert img.size == (64, 48)
    r = f.richieste[0]
    assert "models/gemini-2.5-flash-image:generateContent" in r["url"] and r["headers"]["x-goog-api-key"] == "g-key"
    assert f.json()["generationConfig"]["imageConfig"]["aspectRatio"] == "9:16"
    testo = cloud.describe("gemini", Image.new("RGB", (10, 10), (255, 0, 0)), "what is it?")
    assert testo == "A red square."
    corpo = f.json(1)
    assert "generationConfig" not in corpo and corpo["contents"][0]["parts"][0]["text"] == "what is it?"


def test_gemini_without_image_explains(finto):
    cloud.imposta("gemini", chiave="g-key")
    finto((200, {"candidates": [{"content": {"parts": [{"text": "I cannot draw that."}]}}]}))
    with pytest.raises(cloud.CloudError, match="cannot draw"):
        cloud.generate("gemini", "x", 512, 512)


def test_stability_generate_edit_inpaint_upscale_cutout(finto):
    cloud.imposta("stability", chiave="st-key")
    rgba = io.BytesIO()
    im = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    im.paste((255, 0, 0, 255), (8, 8, 24, 24))
    im.save(rgba, "PNG")
    f = finto((200, _png()), (200, _png()), (200, _png()), (200, _png(size=(128, 96))), (200, rgba.getvalue()))
    cloud.generate("stability", "a castle", 1920, 1080, negative="blurry", seed=7)
    cloud.edit("stability", Image.new("RGB", (64, 48)), "winter", strength=0.3)
    cloud.inpaint("stability", Image.new("RGB", (64, 48)), Image.new("L", (64, 48), 255), "grass")
    up = cloud.upscale("stability", Image.new("RGB", (32, 24)))
    m = cloud.cutout("stability", Image.new("RGB", (32, 32)))
    urls = [r["url"] for r in f.richieste]
    assert urls == ["https://api.stability.ai/v2beta/stable-image/generate/core",
                    "https://api.stability.ai/v2beta/stable-image/generate/sd3",
                    "https://api.stability.ai/v2beta/stable-image/edit/inpaint",
                    "https://api.stability.ai/v2beta/stable-image/upscale/fast",
                    "https://api.stability.ai/v2beta/stable-image/edit/remove-background"]
    assert f.richieste[0]["headers"]["Accept"] == "image/*"
    corpo = f.richieste[0]["body"]
    assert b"16:9" in corpo and b"blurry" in corpo and b'name="seed"\r\n\r\n7' in corpo
    assert b"image-to-image" in f.richieste[1]["body"] and b"0.30" in f.richieste[1]["body"]
    assert up.size == (128, 96)
    assert m.getpixel((16, 16)) == 255 and m.getpixel((2, 2)) == 0


def test_openrouter_image_from_chat(finto):
    cloud.imposta("openrouter", chiave="or-key")
    f = finto((200, {"choices": [{"message": {"content": "", "images": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _b64()}}]}}]}))
    out = cloud.edit("openrouter", Image.new("RGB", (100, 50)), "make it night")
    corpo = f.json()
    assert corpo["modalities"] == ["image", "text"] and corpo["model"] == "google/gemini-2.5-flash-image"
    assert corpo["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert out.size == (100, 50)


def test_replicate_polls_until_done(finto, monkeypatch):
    monkeypatch.setattr(cloud.time, "sleep", lambda s: None)
    cloud.imposta("replicate", chiave="r8-key")
    f = finto((201, {"id": "p1", "status": "starting", "urls": {"get": "https://api.replicate.com/v1/predictions/p1"}}),
              (200, {"id": "p1", "status": "succeeded", "output": ["https://replicate.delivery/x.png"]}),
              (200, _png()))
    img = cloud.generate("replicate", "a lighthouse", 1024, 1024, seed=3)
    assert f.richieste[0]["url"] == "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions"
    assert f.json()["input"] == {"prompt": "a lighthouse", "aspect_ratio": "1:1", "output_format": "png", "seed": 3}
    assert f.richieste[1]["method"] == "GET" and f.richieste[2]["url"] == "https://replicate.delivery/x.png"
    assert img.size == (64, 48)


def test_replicate_kontext_uses_input_image(finto):
    cloud.imposta("replicate", chiave="r8-key")
    f = finto((201, {"status": "succeeded", "output": "data:image/png;base64," + _b64()}))
    cloud.edit("replicate", Image.new("RGB", (20, 20)), "sketch style")
    assert "input_image" in f.json()["input"]


def test_anthropic_describe(finto):
    cloud.imposta("anthropic", chiave="sk-ant")
    f = finto((200, {"content": [{"type": "text", "text": "A gradient."}]}))
    assert cloud.describe("anthropic", Image.new("RGB", (8, 8))) == "A gradient."
    r = f.richieste[0]
    assert r["url"] == "https://api.anthropic.com/v1/messages" and r["headers"]["x-api-key"] == "sk-ant"
    assert f.json()["messages"][0]["content"][0]["source"]["media_type"] == "image/png"


def test_custom_openai_compatible_address(finto):
    cloud.imposta("custom", chiave="k", indirizzo="http://localhost:8080/v1/", modelli={"generate": "flux"})
    f = finto((200, {"data": [{"b64_json": _b64()}]}))
    cloud.generate("custom", "x", 512, 512)
    assert f.richieste[0]["url"] == "http://localhost:8080/v1/images/generations"


def test_errors_are_readable(finto):
    cloud.imposta("openai", chiave="sk-bad")
    finto((401, {"error": {"message": "Incorrect API key provided"}}))
    with pytest.raises(cloud.CloudError, match="密钥无效.*Incorrect API key"):
        cloud.generate("openai", "x", 512, 512)
    finto((429, {"error": {"message": "quota"}}))
    with pytest.raises(cloud.CloudError, match="速率或配额限制"):
        cloud.generate("openai", "x", 512, 512)


# --------------------------------------------------------------------------
# 从编辑器发起。
# --------------------------------------------------------------------------


def test_store_generate_with_a_named_service(store, finto):
    cloud.imposta("openai", chiave="sk-test")
    finto((200, {"data": [{"b64_json": _b64(size=(1024, 1024))}]}))
    v = store.generate_image("a lake", backend="openai")
    assert v["backend"] == "openai" and v["type"] == "image"


def test_store_inpaint_uses_default_service_with_prompt(store, finto):
    store.add_fill(color="#808080")
    cloud.imposta("stability", chiave="st", default=True)
    f = finto((200, _png(size=(300, 200))))
    v = store.inpaint([100, 50, 60, 60], prompt="a red ball")
    assert v["backend"] == "stability" and "edit/inpaint" in f.richieste[0]["url"]
    # 没有提示词时继续使用免费的本地物体移除。
    v = store.inpaint([10, 10, 20, 20])
    assert v["backend"] in ("opencv", "diffusion") and len(f.richieste) == 1


def test_store_describe_and_errors(store, finto):
    store.add_fill(color="#ff0000")
    with pytest.raises(EditError, match="视觉模型"):
        store.describe_image()
    cloud.imposta("anthropic", chiave="sk-ant")
    finto((200, {"content": [{"type": "text", "text": "Red."}]}))
    assert store.describe_image() == {"text": "Red.", "backend": "anthropic"}


def test_store_expand_with_service(store, finto, photo):
    store.add_image(path=photo, fit="cover")
    cloud.imposta("openai", chiave="sk-test")
    f = finto((200, {"data": [{"b64_json": _b64(size=(1536, 1024))}]}))
    v = store.expand_canvas(left=100, right=100, backend="openai")
    assert v["backend"] == "openai" and v["width"] == 600
    assert b"continue the scene" in f.richieste[0]["body"]


def test_ai_status_lists_online_services():
    cloud.imposta("gemini", chiave="g")
    st = ai.status(check_comfy=False)
    assert "gemini" in st["online"]["ready"] and st["online"]["default"] is None


def test_unknown_backend_is_explained(store):
    with pytest.raises(EditError, match="未知后端"):
        store.generate_image("x", backend="midjourney")
