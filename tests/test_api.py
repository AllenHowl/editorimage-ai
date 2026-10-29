"""界面 API 测试：通过 HTTP 调用相同的 Store 操作。"""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from pedit import api


@pytest.fixture
def client(tmp_path):
    api.S.store = None
    api.S.chat.clear()
    api.S._cache.clear()
    with TestClient(api.app) as c:
        yield c
    api.S.store = None


def test_state_without_project(client):
    r = client.get("/api/state").json()
    assert r["project"] is None and any(e["name"] == "blur" for e in r["effects"])
    for effect in r["effects"]:
        assert any("\u4e00" <= c <= "\u9fff" for c in effect["label"])
        assert any("\u4e00" <= c <= "\u9fff" for c in effect["desc"])
        for param in effect["params"].values():
            assert any("\u4e00" <= c <= "\u9fff" for c in param["desc"])
    assert next(look for look in r["looks"] if look["name"] == "warm")["label"] == "暖色"


def test_create_op_render(client, tmp_path, photo):
    r = client.post("/api/project/create", json={"width": 200, "height": 100, "path": str(tmp_path / "d.json")})
    assert r.status_code == 200 and r.json()["project"]["width"] == 200
    r = client.post("/api/op/add_image", json={"path": photo})
    assert r.status_code == 200
    lid = r.json()["result"]["id"]
    rev = r.json()["revision"]
    img = client.get(f"/api/render?w=100&rev={rev}")
    assert img.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(img.content)).size == (100, 50)
    th = client.get(f"/api/layer/{lid}/thumb")
    assert th.status_code == 200
    r = client.post("/api/op/undo", json={})
    assert r.json()["revision"] != rev


def test_bad_op(client):
    client.post("/api/project/create", json={"width": 10, "height": 10})
    assert client.post("/api/op/nope", json={}).status_code == 404
    r = client.post("/api/op/set_layer", json={"layer": "zz", "opacity": 0.5})
    assert r.status_code == 400 and "未找到图层" in r.json()["detail"]
    r = client.post("/api/op/add_text", json={"bogus": 1})
    assert r.status_code == 400


def test_upload_and_export(client, tmp_path, photo):
    client.post("/api/project/create", json={"width": 240, "height": 120})
    with open(photo, "rb") as f:
        r = client.post("/api/upload", files=[("files", ("p.png", f, "image/png"))], data={"add": "1"})
    assert r.status_code == 200 and len(r.json()["project"]["layers"]) == 1
    out = tmp_path / "o.png"
    r = client.post("/api/export", json={"path": str(out)})
    assert r.status_code == 200 and out.exists()


def test_browse(client, tmp_path, photo):
    r = client.get(f"/api/browse?path={tmp_path}").json()
    assert any(e["name"] == "photo.png" for e in r["entries"])


def test_review_and_analyze(client):
    client.post("/api/project/create", json={"width": 100, "height": 100})
    assert client.get("/api/review").json()["ok"] is False      # 空文档
    client.post("/api/op/add_fill", json={"color": "#808080"})
    a = client.get("/api/analyze").json()
    assert abs(a["brightness"] - 0.5) < 0.05


def test_non_local_host_refused(client):
    api._host_consentiti.update({"127.0.0.1", "localhost"})
    try:
        r = client.get("/api/state", headers={"host": "evil.example"})
        assert r.status_code == 403
        r = client.get("/api/state", headers={"host": "127.0.0.1:8770", "origin": "http://evil.example"})
        assert r.status_code == 403
    finally:
        api._host_consentiti.clear()


def test_export_question_without_ui_is_refused():
    api.S.listeners.clear()
    assert api.conferma_export("x.png") is False


def test_ai_services_settings(client):
    r = client.post("/api/ai/services", json={"provider": "stability", "key": "sk-secret-123456789", "default": True})
    assert r.status_code == 200 and r.json()["default"] == "stability"
    st = client.get("/api/ai/services").json()
    assert "secret" not in str(st)
    r = client.post("/api/ai/services", json={"provider": "nope"})
    assert r.status_code == 400


def test_group_ops_from_ui(client):
    client.post("/api/project/create", json={"width": 100, "height": 100})
    a = client.post("/api/op/add_shape", json={"w": 10, "h": 10}).json()["result"]["id"]
    r = client.post("/api/op/group_layers", json={"layers": [a]})
    g = r.json()["result"]["id"]
    assert r.json()["project"]["layers"][0]["children"][0]["id"] == a
    assert client.get(f"/api/layer/{a}/thumb").status_code == 200
    assert client.get(f"/api/layer/{g}/thumb").status_code == 200
