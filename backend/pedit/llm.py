"""助手模型：模型选择、密钥及通信方式。

三类引擎向聊天模块提供统一的 ``text``、``tool``、``tool_done``、
``tool_error``、``error``、``done`` 事件流：
- ``anthropic``：通过官方 SDK 调用 Claude。
- ``openai``：兼容 OpenAI chat/completions 协议的服务，包括 OpenAI、
  Gemini、OpenRouter、Groq、Mistral、DeepSeek、xAI，以及本地 Ollama
  和 LM Studio，仅需地址和密钥。
- ``claude_code``：非交互式调用本机 Claude Code，无需密钥，使用已登录
  用户的订阅，通过编辑器的 ``/mcp`` 服务操作浏览器中显示的同一文档。

密钥仅保存在本机 ``~/.pedit/llm.json``，不会写入可分享的项目文件。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterator

# --------------------------------------------------------------------------
# 模型目录
# --------------------------------------------------------------------------

PROVIDERS: list[dict] = [
    {"id": "claude_code", "nome": "Claude Code", "tipo": "claude_code", "chiave": False,
     "modello": "", "modelli": ["", "opus", "sonnet", "haiku"],
     "nota": "使用本机安装的 Claude Code 和你的订阅，"
             "无需粘贴密钥。"},
    {"id": "anthropic", "nome": "Anthropic (Claude)", "tipo": "anthropic", "chiave": True,
     "env": "ANTHROPIC_API_KEY", "modello": "claude-opus-5-5",
     "modelli": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"],
     "nota": "从 console.anthropic.com 获取密钥。"},
    {"id": "openai", "nome": "OpenAI", "tipo": "openai", "chiave": True, "env": "OPENAI_API_KEY",
     "base": "https://api.openai.com/v1", "modello": "gpt-5", "modelli": ["gpt-5", "gpt-5-mini"],
     "nota": "从 platform.openai.com 获取密钥。"},
    {"id": "gemini", "nome": "Google Gemini", "tipo": "openai", "chiave": True, "env": "GEMINI_API_KEY",
     "base": "https://generativelanguage.googleapis.com/v1beta/openai", "modello": "gemini-2.5-pro",
     "modelli": ["gemini-2.5-pro", "gemini-2.5-flash"], "nota": "从 aistudio.google.com 获取密钥。"},
    {"id": "openrouter", "nome": "OpenRouter", "tipo": "openai", "chiave": True,
     "env": "OPENROUTER_API_KEY", "base": "https://openrouter.ai/api/v1",
     "modello": "anthropic/claude-opus-5-5", "modelli": [],
     "nota": "一个密钥可调用数百种模型：openrouter.ai"},
    {"id": "groq", "nome": "Groq", "tipo": "openai", "chiave": True, "env": "GROQ_API_KEY",
     "base": "https://api.groq.com/openai/v1", "modello": "", "modelli": []},
    {"id": "mistral", "nome": "Mistral", "tipo": "openai", "chiave": True, "env": "MISTRAL_API_KEY",
     "base": "https://api.mistral.ai/v1", "modello": "mistral-large-latest", "modelli": []},
    {"id": "deepseek", "nome": "DeepSeek", "tipo": "openai", "chiave": True, "env": "DEEPSEEK_API_KEY",
     "base": "https://api.deepseek.com/v1", "modello": "deepseek-chat", "modelli": []},
    {"id": "xai", "nome": "xAI (Grok)", "tipo": "openai", "chiave": True, "env": "XAI_API_KEY",
     "base": "https://api.x.ai/v1", "modello": "", "modelli": []},
    {"id": "ollama", "nome": "Ollama（本机）", "tipo": "openai", "chiave": False,
     "base": "http://localhost:11434/v1", "modello": "", "modelli": [],
     "nota": "本地模型：需要选择支持工具调用的模型。"},
    {"id": "lmstudio", "nome": "LM Studio（本机）", "tipo": "openai", "chiave": False,
     "base": "http://localhost:1234/v1", "modello": "", "modelli": []},
    {"id": "custom", "nome": "其他（兼容 OpenAI）", "tipo": "openai", "chiave": True,
     "base": "", "modello": "", "modelli": [],
     "nota": "支持提供 /chat/completions 接口的服务，请粘贴地址和密钥。"},
]

MAX_TURNS = 16
MAX_IMMAGINI = 6


def provider(pid: str) -> dict:
    p = next((p for p in PROVIDERS if p["id"] == pid), None)
    if p is None:
        raise ValueError(f"未知服务商：{pid}")
    return p


# --------------------------------------------------------------------------
# 配置（仅保存在本机）。
# --------------------------------------------------------------------------


def _file() -> Path:
    base = Path(os.environ.get("PEDIT_HOME") or (Path.home() / ".pedit"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "llm.json"


def carica() -> dict:
    try:
        cfg = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("provider", "")
    for k in ("chiavi", "modelli", "indirizzi"):
        cfg.setdefault(k, {})
    return cfg


def salva(cfg: dict) -> None:
    f = _file()
    tmp = f.with_name(f"~{f.name}")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, f)
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass


def imposta(provider_id: str, chiave: str | None = None, modello: str | None = None,
            indirizzo: str | None = None, attiva: bool = True) -> dict:
    """更新配置。``chiave=""`` 删除密钥，``None`` 保持原值。"""
    provider(provider_id)
    cfg = carica()
    if chiave is not None:
        if chiave.strip():
            cfg["chiavi"][provider_id] = chiave.strip()
        else:
            cfg["chiavi"].pop(provider_id, None)
    if modello is not None:
        cfg["modelli"][provider_id] = modello.strip()
    if indirizzo is not None:
        cfg["indirizzi"][provider_id] = indirizzo.strip().rstrip("/")
    if attiva:
        cfg["provider"] = provider_id
    salva(cfg)
    return stato()


def _chiave(p: dict, cfg: dict) -> str:
    return cfg["chiavi"].get(p["id"]) or (os.environ.get(p["env"], "") if p.get("env") else "")


def _modello(p: dict, cfg: dict) -> str:
    return cfg["modelli"].get(p["id"]) or p.get("modello", "")


def _indirizzo(p: dict, cfg: dict) -> str:
    return (cfg["indirizzi"].get(p["id"]) or p.get("base", "")).rstrip("/")


def claude_exe() -> str | None:
    return os.environ.get("PEDIT_CLAUDE") or shutil.which("claude")


def _pronto(p: dict, cfg: dict) -> str | None:
    """服务可用时返回 None，否则返回原因。"""
    if p["tipo"] == "claude_code":
        return None if claude_exe() else (
            "本机尚未安装 Claude Code："
            "运行 npm install -g @anthropic-ai/claude-code，然后运行一次 claude 完成登录")
    if p["tipo"] == "anthropic":
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return "缺少 anthropic 包：pip install anthropic"
        if not _chiave(p, cfg) and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return "请粘贴 Anthropic API 密钥"
        return None
    if p["chiave"] and not _chiave(p, cfg):
        return f"请粘贴 {p['nome']} API 密钥"
    if not _indirizzo(p, cfg):
        return "缺少服务地址"
    if not _modello(p, cfg):
        return "请输入模型名称"
    return None


def _maschera(k: str) -> str:
    return "" if not k else (k[:4] + "…" + k[-4:] if len(k) > 12 else "••••")


def stato() -> dict:
    """返回界面可见的状态，绝不返回完整密钥。"""
    cfg = carica()
    elenco = []
    for p in PROVIDERS:
        k = _chiave(p, cfg)
        elenco.append({
            "id": p["id"], "nome": p["nome"], "tipo": p["tipo"], "vuole_chiave": p["chiave"],
            "nota": p.get("nota", ""), "modello": _modello(p, cfg), "modelli": p.get("modelli", []),
            "indirizzo": _indirizzo(p, cfg), "indirizzo_modificabile": p["tipo"] == "openai",
            "chiave": _maschera(k), "chiave_da_ambiente": bool(k) and p["id"] not in cfg["chiavi"],
            "pronto": _pronto(p, cfg) is None, "motivo": _pronto(p, cfg),
        })
    attivo = cfg["provider"] or _scelta_automatica(cfg)
    a = next((e for e in elenco if e["id"] == attivo), None)
    return {
        "provider": attivo, "providers": elenco,
        "ok": bool(a and a["pronto"]),
        "model": a["modello"] if a else "", "nome": a["nome"] if a else "",
        "motivo": (a["motivo"] if a else "请在助手设置中选择模型"),
    }


def _scelta_automatica(cfg: dict) -> str:
    """没有保存过选择时，使用首个已可用的服务。"""
    for pid in ("anthropic", "openai", "gemini", "openrouter", "claude_code"):
        p = provider(pid)
        if _pronto(p, cfg) is None:
            return pid
    return ""


def attivo() -> tuple[dict, dict]:
    cfg = carica()
    pid = cfg["provider"] or _scelta_automatica(cfg)
    if not pid:
        raise RuntimeError("尚未配置模型，请打开助手设置")
    p = provider(pid)
    motivo = _pronto(p, cfg)
    if motivo:
        raise RuntimeError(motivo)
    return p, cfg


# --------------------------------------------------------------------------
# 引擎：Anthropic。
# --------------------------------------------------------------------------


def _mai() -> bool:
    return False


def _blocchi(out: Any) -> Any:
    """将工具结果的文本或（文本、图片）转换为包含图片的内容块。"""
    if not isinstance(out, tuple):
        return out
    testo, immagini = out
    blocchi: list[dict] = [{"type": "text", "text": testo or "ok"}]
    for im in immagini:
        blocchi.append({"type": "image", "source": {"type": "base64", "media_type": im.get("mime", "image/png"),
                                                    "data": im["b64"]}})
    return blocchi


def run_anthropic(p: dict, cfg: dict, system: str, tools: list[dict], history: list,
                  contenuto: list[dict], run_op: Callable[[str, dict], str],
                  fermo: Callable[[], bool] = _mai) -> Iterator[dict]:
    import anthropic

    kw: dict[str, Any] = {}
    k = cfg["chiavi"].get("anthropic")
    if k:
        kw["api_key"] = k
    cl = anthropic.Anthropic(**kw)
    model = _modello(p, cfg) or "claude-opus-5-5"
    strumenti = [{**t, "eager_input_streaming": True} for t in tools]

    blocchi = []
    for c in contenuto:
        if c["type"] == "image":
            blocchi.append({"type": "image", "source": {"type": "base64",
                                                        "media_type": c.get("mime", "image/png"),
                                                        "data": c["b64"]}})
        elif c["type"] == "document":
            blocchi.append({"type": "document", "source": {"type": "base64",
                                                           "media_type": "application/pdf",
                                                           "data": c["b64"]}})
        else:
            blocchi.append({"type": "text", "text": c["text"]})
    history.append({"role": "user", "content": blocchi})

    for _ in range(MAX_TURNS):
        try:
            with cl.beta.messages.stream(
                model=model,
                max_tokens=64000,
                system=system,
                tools=strumenti,
                # API 无状态，每次请求都要发送系统提示、工具和历史，
                # 命中缓存时费用约为原来的十分之一。
                cache_control={"type": "ephemeral"},
                messages=history,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                betas=["server-side-fallback-2026-07-01"],
                extra_body={"fallbacks": "default"},
            ) as stream:
                fermato = False
                for event in stream:
                    if fermo():
                        fermato = True
                        break
                    if event.type == "content_block_start" and event.content_block.type == "thinking":
                        yield {"type": "thinking"}
                    elif event.type == "content_block_delta" and event.delta.type == "text_delta":
                        yield {"type": "text", "text": event.delta.text}
                if not fermato:
                    message = stream.get_final_message()
            if fermato:
                # 未完成的回复不写入历史，改为添加说明，
                # 使下一轮从有效的对话状态继续。
                history.append({"role": "assistant", "content": [{"type": "text", "text": "（用户已停止）"}]})
                yield {"type": "stopped"}
                return
        except anthropic.AuthenticationError:
            yield {"type": "error", "message": "Anthropic 密钥无效"}
            return
        except anthropic.RateLimitError:
            yield {"type": "error", "message": "请求过多，请稍后重试"}
            return
        except anthropic.APIStatusError as exc:
            yield {"type": "error", "message": f"服务错误（{exc.status_code}): {exc.message}"}
            return
        except anthropic.APIConnectionError:
            yield {"type": "error", "message": "无法连接 Anthropic，请检查网络"}
            return

        history.append({"role": "assistant", "content": message.content})
        if message.stop_reason == "refusal":
            yield {"type": "error", "message": "请求被拒绝"}
            return
        if message.stop_reason == "max_tokens":
            yield {"type": "error", "message": "回复过长，已截断"}
            return
        if message.stop_reason != "tool_use":
            yield {"type": "done"}
            return

        risultati = []
        for b in message.content:
            if b.type != "tool_use":
                continue
            # 每个 tool_use 都必须有对应结果，停止时也不例外。
            if fermo():
                risultati.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                  "content": "（用户已停止）"})
                continue
            # 启用 eager_input_streaming 后，服务不会校验输入。
            if not isinstance(b.input, dict):
                risultati.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                  "content": "INVALID_JSON：无法解析参数，请重试"})
                continue
            yield {"type": "tool", "name": b.name, "input": b.input}
            try:
                out = run_op(b.name, dict(b.input))
                risultati.append({"type": "tool_result", "tool_use_id": b.id, "content": _blocchi(out)})
                yield {"type": "tool_done", "name": b.name}
            except Exception as exc:
                risultati.append({"type": "tool_result", "tool_use_id": b.id,
                                  "content": f"错误：{exc}", "is_error": True})
                yield {"type": "tool_error", "name": b.name, "message": str(exc)}
        history.append({"role": "user", "content": risultati})
        if fermo():
            history.append({"role": "assistant", "content": [{"type": "text", "text": "（用户已停止）"}]})
            yield {"type": "stopped"}
            return
    yield {"type": "error", "message": "操作步骤过多，已停止"}


# --------------------------------------------------------------------------
# 引擎：OpenAI 兼容服务（OpenAI、Gemini、OpenRouter、Ollama 等）。
# --------------------------------------------------------------------------


def strumenti_openai(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {
        "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
        for t in tools]


def _post_stream(url: str, chiave: str, body: dict) -> Iterator[dict]:
    """发送流式 POST 请求，为 SSE 流中的每条 ``data:`` 返回一个字典。"""
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if chiave:
        headers["Authorization"] = f"Bearer {chiave}"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=300) as res:
        for raw in res:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                yield json.loads(data)
            except ValueError:
                continue


def _errore_http(exc: urllib.error.HTTPError) -> str:
    try:
        corpo = exc.read().decode("utf-8", "replace")
        d = json.loads(corpo)
        msg = (d.get("error") or {}).get("message") if isinstance(d.get("error"), dict) else d.get("error")
        corpo = msg or corpo
    except Exception:  # noqa: BLE001
        corpo = ""
    if exc.code in (401, 403):
        return "密钥无效或权限不足"
    return f"服务错误（{exc.code}): {str(corpo)[:300]}"


def run_openai(p: dict, cfg: dict, system: str, tools: list[dict], history: list,
               contenuto: list[dict], run_op: Callable[[str, dict], str],
               fermo: Callable[[], bool] = _mai) -> Iterator[dict]:
    url = _indirizzo(p, cfg) + "/chat/completions"
    chiave = _chiave(p, cfg)
    model = _modello(p, cfg)
    fn = strumenti_openai(tools)

    parti = []
    for c in contenuto:
        if c["type"] == "image":
            mime = c.get("mime", "image/png")
            parti.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64," + c["b64"]}})
        else:
            parti.append({"type": "text", "text": c["text"]})
    solo_testo = "\n\n".join(c["text"] for c in contenuto if c["type"] == "text")
    history.append({"role": "user", "content": parti})

    for _ in range(MAX_TURNS):
        testo, chiamate, fine, fermato = "", {}, None, False
        body = {"model": model, "stream": True, "tools": fn,
                "messages": [{"role": "system", "content": system}] + history}
        try:
            try:
                for ev in _post_stream(url, chiave, body):
                    if fermo():
                        fermato = True
                        break
                    for ch in ev.get("choices") or []:
                        d = ch.get("delta") or {}
                        if d.get("content"):
                            testo += d["content"]
                            yield {"type": "text", "text": d["content"]}
                        for tc in d.get("tool_calls") or []:
                            slot = chiamate.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                            slot["id"] = tc.get("id") or slot["id"]
                            f = tc.get("function") or {}
                            slot["name"] += f.get("name") or ""
                            slot["args"] += f.get("arguments") or ""
                        fine = ch.get("finish_reason") or fine
            except urllib.error.HTTPError as exc:
                # 部分模型不支持图片，失败后仅携带文本重试。
                if exc.code == 400 and isinstance(history[-1].get("content"), list) and any(
                        x.get("type") == "image_url" for x in history[-1]["content"]):
                    history[-1] = {"role": "user", "content": solo_testo}
                    yield {"type": "note", "message": "当前模型无法查看图片，"
                                                      "引用将仅以文字形式发送"}
                    continue
                raise
        except urllib.error.HTTPError as exc:
            yield {"type": "error", "message": _errore_http(exc)}
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            yield {"type": "error", "message": f"无法连接 {p['nome']}: {exc}"}
            return

        if fermato:
            history.append({"role": "assistant", "content": testo or "（用户已停止）"})
            yield {"type": "stopped"}
            return
        msg: dict[str, Any] = {"role": "assistant", "content": testo or None}
        if chiamate:
            msg["tool_calls"] = [{"id": c["id"] or f"call_{i}", "type": "function",
                                  "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                                 for i, c in sorted(chiamate.items())]
        history.append(msg)
        if not chiamate:
            yield {"type": "done"}
            return

        for tc in msg["tool_calls"]:
            nome = tc["function"]["name"]
            if fermo():
                history.append({"role": "tool", "tool_call_id": tc["id"], "content": "（用户已停止）"})
                continue
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
                if not isinstance(args, dict):
                    raise ValueError("参数必须为对象")
            except ValueError as exc:
                history.append({"role": "tool", "tool_call_id": tc["id"],
                                "content": f"INVALID_JSON: {exc}"})
                continue
            yield {"type": "tool", "name": nome, "input": args}
            try:
                out = run_op(nome, args)
                if isinstance(out, tuple):
                    # chat/completions 的工具结果不支持图片，仅保留文字。
                    out = out[0] + ("（已生成图片，但当前模型无法查看）" if out[1] else "")
                yield {"type": "tool_done", "name": nome}
            except Exception as exc:
                out = f"错误：{exc}"
                yield {"type": "tool_error", "name": nome, "message": str(exc)}
            history.append({"role": "tool", "tool_call_id": tc["id"], "content": out})
        if fermo():
            yield {"type": "stopped"}
            return
    yield {"type": "error", "message": "操作步骤过多，已停止"}


# --------------------------------------------------------------------------
# 引擎：本机安装的 Claude Code。
# --------------------------------------------------------------------------

PREFISSO_MCP = "mcp__pedit__"

SISTEMA_CLAUDE_CODE = """你正在 pedit 内工作，这是用户已在浏览器中打开的图层式图片编辑器。
通过 mcp__pedit__ 工具操作当前文档，用 project_info 查看状态。无需传入 project 参数。
每项修改都会立即显示，并可按 Ctrl+Z 撤销。

所有编辑均使用 pedit 工具。宣布完成前用 mcp__pedit__preview 检查结果，
grid=true 可查看像素坐标。保持非破坏式编辑：使用调整图层、蒙版和新图层补丁。
完成前调用 review_image。

禁止自行导出，仅在用户要求时调用 export_image，编辑器还会请求用户确认。
用户指定区域时，引用图片以 PNG 文件提供，可用 Read 打开。
默认使用简体中文简洁回答；用户明确指定其他语言时遵从其要求。"""


def _comando_claude(exe: str) -> list[str]:
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe]
    return [exe]


def run_claude_code(p: dict, cfg: dict, stato_chat: dict, contenuto: list[dict], mcp_url: str,
                    cwd: str | None, fermo: Callable[[], bool] = _mai) -> Iterator[dict]:
    exe = claude_exe()
    if not exe:
        yield {"type": "error", "message": _pronto(p, cfg)}
        return
    config_mcp = json.dumps({"mcpServers": {"pedit": {"type": "http", "url": mcp_url}}})
    args = _comando_claude(exe) + [
        "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        "--mcp-config", config_mcp, "--strict-mcp-config",
        # pedit 以外仅允许读取：附件图片和 PDF、指定目录及链接。
        "--allowedTools", "mcp__pedit", "Read", "Glob", "WebFetch",
        "--append-system-prompt", SISTEMA_CLAUDE_CODE,
    ]
    modello = _modello(p, cfg)
    if modello:
        args += ["--model", modello]
    if stato_chat.get("sessione"):
        args += ["--resume", stato_chat["sessione"]]

    prompt = "\n\n".join(c["text"] if c["type"] == "text" else f"（图片：{c['path']})"
                         for c in contenuto)
    env = {**os.environ}
    env.pop("CLAUDECODE", None)
    # ask_user 会等待用户在编辑器中回答，可能需要较长时间。
    env.setdefault("MCP_TOOL_TIMEOUT", "1800000")  # 从其他会话中启动时，不应被识别为嵌套会话。
    try:
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd or None, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        yield {"type": "error", "message": f"无法启动 Claude Code：{exc}"}
        return
    # 读取输出会阻塞，另开线程监听停止信号，
    # 用户单击“停止”时终止进程。
    import threading
    import time

    fermato = threading.Event()

    def sorveglia() -> None:
        while proc.poll() is None:
            if fermo():
                fermato.set()
                proc.kill()
                return
            time.sleep(0.2)

    threading.Thread(target=sorveglia, daemon=True).start()
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
        aperti: dict[str, str] = {}
        scritto = False   # 已有文字时，将新内容块与前一块分开。
        for raw in proc.stdout:
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            tipo = ev.get("type")
            if ev.get("session_id"):
                stato_chat["sessione"] = ev["session_id"]
            if tipo == "stream_event":
                e = ev.get("event") or {}
                d = e.get("delta") or {}
                if e.get("type") == "content_block_delta" and d.get("type") == "text_delta":
                    scritto = True
                    yield {"type": "text", "text": d.get("text", "")}
                elif e.get("type") == "content_block_start" and scritto and \
                        (e.get("content_block") or {}).get("type") == "text":
                    yield {"type": "text", "text": "\n\n"}
                elif e.get("type") == "content_block_start" and (e.get("content_block") or {}).get("type") == "thinking":
                    yield {"type": "thinking"}
            elif tipo == "assistant":
                for b in (ev.get("message") or {}).get("content") or []:
                    if b.get("type") == "tool_use":
                        grezzo = b.get("name", "")
                        # Claude Code 内部步骤（如搜索工具）无需展示给修图用户，
                        # 仅显示 pedit 和 Read 操作。
                        if not grezzo.startswith(PREFISSO_MCP) and grezzo not in ("Read", "Glob", "WebFetch"):
                            continue
                        nome = grezzo.removeprefix(PREFISSO_MCP)
                        aperti[b.get("id", "")] = nome
                        yield {"type": "tool", "name": nome, "input": b.get("input") or {}}
            elif tipo == "user":
                for b in (ev.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        if b.get("tool_use_id", "") not in aperti:
                            continue
                        nome = aperti.pop(b.get("tool_use_id", ""))
                        if b.get("is_error"):
                            testo = b.get("content")
                            if isinstance(testo, list):
                                testo = " ".join(x.get("text", "") for x in testo if isinstance(x, dict))
                            yield {"type": "tool_error", "name": nome, "message": str(testo)[:300]}
                        else:
                            yield {"type": "tool_done", "name": nome}
            elif tipo == "result":
                if ev.get("is_error"):
                    yield {"type": "error", "message": str(ev.get("result") or ev.get("subtype"))[:400]}
                else:
                    yield {"type": "done"}
        code = proc.wait()
        if fermato.is_set():
            yield {"type": "stopped"}
            return
        if code != 0:
            err = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace").strip()
            if err:
                yield {"type": "error", "message": f"Claude Code: {err[-400:]}"}
    finally:
        if proc.poll() is None:
            proc.kill()


# --------------------------------------------------------------------------
# 引用区域的图片。
# --------------------------------------------------------------------------


def immagine(path: str) -> dict:
    mime = mimetypes.guess_type(path)[0] or "image/png"
    if mime == "image/jpg":
        mime = "image/jpeg"
    return {"type": "image", "path": path, "mime": mime,
            "b64": base64.b64encode(Path(path).read_bytes()).decode("ascii")}


def documento(path: str) -> dict:
    return {"type": "document", "path": path,
            "b64": base64.b64encode(Path(path).read_bytes()).decode("ascii")}
