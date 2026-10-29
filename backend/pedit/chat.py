"""编辑器内置聊天助手。

工具直接复用 MCP 服务的 ``mcp_server.STRUMENTI`` 及其参数结构，
只需维护一份列表。内置助手和外部智能体调用相同操作，
所有修改均通过 ``Store``，实时显示在编辑器中，并可按 Ctrl+Z 撤销。
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
from pathlib import Path
from typing import Any, Callable, Iterator

from . import llm
from . import mcp_server as ms

SYSTEM = """你是 pedit 内置的修图助手。pedit 是类似 Photoshop 的非破坏式图层编辑器。
你操作的是当前打开的文档，工具与界面按钮调用相同的操作，结果会立即显示，
并可按 Ctrl+Z 撤销。

规则：
- 查看结果：决定位置和边界前使用 preview，grid=true 可查看像素坐标；
  每组修改后再次预览检查。
- 修正前先用 analyze_image 测量，适度调整；先修正曝光和白平衡，再用 apply_look 添加风格。
- 保持非破坏式编辑：使用调整图层、蒙版及新图层补丁。
- 使用 batch 合并已确定的修改，一次调用、一步撤销。
- 宣布完成前调用 review_image。
- 禁止自行导出：仅在用户要求时调用 export_image。
- 先用 ai_status 确认可用能力，不承诺未配置的抠图或 ComfyUI 功能。

默认使用简体中文简洁回答；用户明确指定其他语言时遵从其要求。不要重复界面已显示的内容。"""

# 界面已经打开，聊天中无需提供这些工具。
_ESCLUSI = {"open_ui", "close_ui", "project_create", "project_open"}


class ChatUnavailable(RuntimeError):
    pass


def available() -> dict:
    return llm.stato()


def build_tools() -> list[dict]:
    """从 MCP 服务提取 Anthropic 格式的工具结构，去掉 ``project`` 参数。"""
    out = []
    for nome in ms.STRUMENTI:
        if nome in _ESCLUSI:
            continue
        t = ms.mcp._tool_manager.get_tool(nome)
        if t is None:
            continue
        schema = json.loads(json.dumps(t.parameters))
        props = {k: v for k, v in (schema.get("properties") or {}).items()
                 if k not in ("project", "contesto_mcp")}
        req = [r for r in schema.get("required") or [] if r in props]
        s2 = {"type": "object", "properties": props}
        if req:
            s2["required"] = req
        if "$defs" in schema:
            s2["$defs"] = schema["$defs"]
        out.append({"name": nome, "description": (t.description or nome)[:1000], "input_schema": s2})
    return out


def execute(store: Any, name: str, args: dict) -> str | tuple[str, list[dict]]:
    """在当前文档上执行工具，图片结果单独返回。"""
    if name in _ESCLUSI or name not in ms.STRUMENTI:
        raise ValueError(f"未知工具：{name}")
    ms._current[0] = store
    fn = ms.STRUMENTI[name]
    args = {k: v for k, v in (args or {}).items() if k != "project"}
    res = asyncio.run(fn(**args)) if inspect.iscoroutinefunction(fn) else fn(**args)
    if isinstance(res, list) and any(isinstance(x, ms.Image) for x in res):
        immagini, testi = [], []
        for x in res:
            if isinstance(x, ms.Image):
                immagini.append({"mime": "image/png", "b64": base64.b64encode(x.data).decode("ascii")})
            else:
                testi.append(x if isinstance(x, str) else ms._json(x))
        return "\n".join(testi), immagini
    return ms._json(res)


def _state_block(store: Any) -> str:
    s = store.summary()
    return "文档状态（图层从上到下排列）：\n" + json.dumps(s, ensure_ascii=False, separators=(",", ":"))


def riferimenti(store: Any, refs: list[dict]) -> tuple[str, list[dict]]:
    """将用户指示的图片区域转换为坐标文字和带选框的图片。"""
    from PIL import ImageDraw

    from . import render

    if not refs:
        return "", []
    righe, immagini = [], []
    p = store.project
    k = min(1.0, 1024 / max(p.width, 1))
    img = render.render(p, store.base_dir, scale=k).convert("RGB")
    d = ImageDraw.Draw(img)
    for i, r in enumerate(refs, 1):
        if r.get("type") == "area":
            x, y, w, h = r["box"]
            righe.append(f"[{i}] 选区 x={x:.0f} y={y:.0f} w={w:.0f} h={h:.0f}（文档像素）")
            d.rectangle([x * k, y * k, (x + w) * k, (y + h) * k], outline=(255, 0, 0), width=3)
            d.text((x * k + 4, y * k + 4), str(i), fill=(255, 0, 0))
        elif r.get("type") == "layer":
            righe.append(f"[{i}] 图层 {r.get('id')} ({r.get('name', '')})")
        elif r.get("type") == "file":
            righe.append(f"[{i}] 附件文件：{r.get('path')}")
            if Path(r.get("path", "")).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                try:
                    immagini.append(llm.immagine(r["path"]))
                except OSError:
                    pass
    if any(r.get("type") == "area" for r in refs):
        import io

        buf = io.BytesIO()
        img.save(buf, "PNG")
        immagini.insert(0, {"type": "image", "mime": "image/png", "path": "(riferimenti)",
                            "b64": base64.b64encode(buf.getvalue()).decode("ascii")})
    return "用户指定的引用：\n" + "\n".join(righe), immagini


def run(store: Any, stato_chat: dict, question: str, refs: list[dict] | None = None,
        mcp_url: str | None = None, fermo: Callable[[], bool] = lambda: False) -> Iterator[dict]:
    """执行一轮聊天，流式返回 text/tool/tool_done/tool_error/error/done 事件。"""
    try:
        p, cfg = llm.attivo()
    except RuntimeError as exc:
        yield {"type": "error", "message": str(exc)}
        return
    testo_rif, immagini = riferimenti(store, refs or [])
    contenuto: list[dict] = [*immagini, {"type": "text", "text": "\n\n".join(
        x for x in (_state_block(store), testo_rif, question) if x)}]
    # 切换引擎后历史格式不兼容，重新开始会话。
    if stato_chat.get("motore") != p["id"]:
        stato_chat.clear()
        stato_chat["motore"] = p["id"]
    history = stato_chat.setdefault("history", [])
    if p["tipo"] == "claude_code":
        if not mcp_url:
            yield {"type": "error", "message": "编辑器的 MCP 服务尚未运行"}
            return
        # Claude Code 通过文件读取图片。
        for c in contenuto:
            if c["type"] == "image" and c.get("path", "").startswith("("):
                f = store.assets_dir() / "chat_riferimenti.png"
                f.write_bytes(base64.b64decode(c["b64"]))
                c["path"] = str(f)
        yield from llm.run_claude_code(p, cfg, stato_chat, contenuto, mcp_url, store.base_dir, fermo)
        return
    tools = build_tools()
    run_op = lambda n, a: execute(store, n, a)  # noqa: E731
    if p["tipo"] == "anthropic":
        yield from llm.run_anthropic(p, cfg, SYSTEM, tools, history, contenuto, run_op, fermo)
    else:
        yield from llm.run_openai(p, cfg, SYSTEM, tools, history, contenuto, run_op, fermo)
