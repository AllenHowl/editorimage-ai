"""MCP 工具：共用 Store 方法，返回紧凑数据和真实图片。"""

import asyncio
import json

import pytest
from mcp.server.mcpserver import Image

from pedit import mcp_server as ms


@pytest.fixture(autouse=True)
def _reset():
    ms._current[0] = None
    ms._stores.clear()
    yield
    ms._current[0] = None


def run(fn, **kw):
    res = fn(**kw)
    return asyncio.run(res) if asyncio.iscoroutine(res) else res


def test_instructions_keep_the_rules():
    for frase in ("查看结果", "非破坏式编辑", "禁止自行导出", "report_issue", "batch"):
        assert frase in ms.ISTRUZIONI


def test_every_tool_has_a_description():
    for nome in ms.STRUMENTI:
        t = ms.mcp._tool_manager.get_tool(nome)
        assert t is not None and len(t.description.strip()) > 3, nome


def test_flow(tmp_path, photo):
    run(ms.project_create, path=str(tmp_path / "d.json"), width=300, height=200)
    img = run(ms.add_image, path=photo)
    t = run(ms.add_text, text="Title", size=40)
    run(ms.align_layer, layer=t["id"], h="center", v="top", margin=10)
    run(ms.add_effect, type="stroke", layer=t["id"], params={"width": 3})
    info = run(ms.project_info)
    assert [ly["id"] for ly in info["layers"]] == [t["id"], img["id"]]
    out = run(ms.preview, width=200, grid=True)
    assert isinstance(out[0], Image) and "300x200" in out[1]
    zoom = run(ms.preview, box=[0, 0, 100, 100], width=200)
    assert isinstance(zoom[0], Image)
    st = run(ms.analyze_image)
    assert 0 <= st["brightness"] <= 1


def test_wrapped_tool_returns_compact_json(tmp_path):
    asyncio.run(ms.mcp.call_tool("project_create", {"width": 50, "height": 40}))
    res = asyncio.run(ms.mcp.call_tool("add_shape", {"w": 10, "h": 10}))
    blocchi = res if isinstance(res, (list, tuple)) else getattr(res, "content", res)
    testo = json.dumps([getattr(b, "text", str(b)) for b in (blocchi[0] if isinstance(blocchi, tuple) else blocchi)])
    assert "shape" in testo and ", " not in testo.replace("\\", "")[:40]


def test_batch_atomic_and_one_undo():
    run(ms.project_create, width=100, height=100)
    out = run(ms.batch, ops=[{"tool": "add_text", "args": {"text": "a"}},
                             {"tool": "add_shape", "args": {"kind": "ellipse"}}])
    assert out["done"] == 2
    s = ms._store()
    assert len(s.project.layers) == 2
    run(ms.undo)
    assert not s.project.layers
    with pytest.raises(Exception):
        run(ms.batch, ops=[{"tool": "add_text", "args": {"text": "a"}}, {"tool": "add_shape", "args": {"kind": "x"}}])
    assert not s.project.layers
    with pytest.raises(Exception):
        run(ms.batch, ops=[{"tool": "export_image", "args": {"path": "x.png"}}])


def test_no_project_error():
    with pytest.raises(Exception, match="尚未打开文档"):
        run(ms.project_info)


def test_report_issue(tmp_path):
    out = run(ms.report_issue, kind="manca", title="clone stamp", details="serve un timbro clone")
    assert "SEGNALAZIONI" in out["saved"]


def test_list_effects():
    lst = run(ms.list_effects)
    assert any(x.startswith("blur") for x in lst)
    one = run(ms.list_effects, name="drop_shadow")
    assert one["kind"] == "style" and "dx" in one["params"]


def test_ai_status_reports_missing_comfy():
    st = run(ms.ai_status)
    assert st["comfyui"]["ok"] is False and "hint" in st["comfyui"]


def test_instructions_mention_new_tools():
    for frase in ("group_layers", "clip=true", "mask_stroke", "edit_image", "describe_image", "费用"):
        assert frase in ms.ISTRUZIONI


def test_groups_clip_mask_and_clone_from_mcp():
    run(ms.project_create, width=200, height=100)
    a = run(ms.add_shape, x=0, y=0, w=100, h=100, fill="#ff0000")
    b = run(ms.add_fill, color="#0000ff")
    run(ms.set_layer, layer=b["id"], clip=True)
    g = run(ms.group_layers, layers=[a["id"], b["id"]], name="Pair")
    info = run(ms.project_info)
    assert info["layers"][0]["type"] == "group" and info["layers"][0]["children"][0]["clip"] is True
    run(ms.mask_stroke, layer=g["id"], points=[[10, 10], [90, 10]], mode="hide", size=8)
    run(ms.paint_stroke, points=[[150, 50]], size=20, source=[50, 50])
    run(ms.move_layer, layer=a["id"], into="root")
    out = run(ms.batch, ops=[{"tool": "ungroup", "args": {"layer": g["id"]}}])
    assert out["done"] == 1


def test_ai_services_without_keys():
    st = run(ms.ai_services)
    ids = {s["id"] for s in st["services"]}
    assert {"openai", "gemini", "stability", "openrouter", "replicate", "anthropic"} <= ids
    assert st["default"] is None and all("chiave" not in s for s in st["services"])
