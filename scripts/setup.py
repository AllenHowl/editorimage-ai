"""从刚克隆的仓库一键安装：``python scripts/setup.py``。

依次安装 Python 依赖、构建网页界面、在 ``.mcp.json`` 注册 MCP 服务，
运行 ``pedit doctor`` 和快速测试。可安全重复执行，已就绪的步骤会跳过。
``--json`` 仅输出一个 JSON 对象，包含 ``result``（ok/warn/fail）及各步骤的
``status``、``detail``、``hint``，供智能体读取。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
FRONT = RADICE / "frontend"
PASSI: list[dict] = []
JSON = False


def log(msg: str) -> None:
    if not JSON:
        print(msg, flush=True)


def passo(nome: str, status: str, detail: str = "", hint: str = "") -> None:
    PASSI.append({"step": nome, "status": status, "detail": detail, "hint": hint})
    segno = {"ok": "ok ", "skip": "-- ", "warn": "!! ", "fail": "XX "}[status]
    log(f"{segno} {nome}: {detail}" + (f"\n    -> {hint}" if hint else ""))


def esegui(cmd: list[str], cwd: Path = RADICE) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, shell=(sys.platform == "win32" and cmd[0] in ("npm", "claude")))
    return p.returncode, (p.stdout + p.stderr)[-2000:]


def python_target(venv: bool) -> str:
    if not venv:
        return sys.executable
    v = RADICE / ".venv"
    exe = v / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not exe.exists():
        code, out = esegui([sys.executable, "-m", "venv", str(v)])
        if code:
            passo("venv", "fail", out[-300:], "create it by hand: python -m venv .venv")
            raise SystemExit(1)
        passo("venv", "ok", str(v))
    return str(exe)


def dipendenze(py: str, extras: str, reinstall: bool) -> None:
    gia = subprocess.run([py, "-c", "import pedit, fastapi, mcp, numpy, PIL"], capture_output=True).returncode == 0
    if gia and not reinstall:
        passo("python dependencies", "skip", "already installed (--reinstall to force)")
        return
    code, out = esegui([py, "-m", "pip", "install", "-e", f".[{extras}]"])
    if code and "externally-managed-environment" in out:
        passo("python dependencies", "warn", "system Python is externally managed: switching to .venv")
        py = python_target(True)
        code, out = esegui([py, "-m", "pip", "install", "-e", f".[{extras}]"])
    if code:
        passo("python dependencies", "fail", out[-400:], "check the pip error above")
        raise SystemExit(1)
    passo("python dependencies", "ok", f"pip install -e .[{extras}]")


def interfaccia(rebuild: bool) -> None:
    if not shutil.which("npm"):
        passo("web UI", "warn", "npm not found", "install Node.js 18+; CLI and MCP work without the UI")
        return
    if (FRONT / "dist" / "index.html").is_file() and not rebuild:
        passo("web UI", "skip", "frontend/dist already built (--rebuild to rebuild)")
        return
    if not (FRONT / "node_modules").is_dir():
        code, out = esegui(["npm", "ci"], FRONT)
        if code:
            passo("web UI", "fail", out[-400:], "cd frontend && npm ci")
            return
    code, out = esegui(["npm", "run", "build"], FRONT)
    passo("web UI", "fail" if code else "ok", out[-300:] if code else "frontend/dist", "cd frontend && npm run build" if code else "")


def registra_mcp(py: str) -> None:
    exe = Path(py).with_name("pedit-mcp.exe" if os.name == "nt" else "pedit-mcp")
    cmd = str(exe) if exe.exists() and py != sys.executable else "pedit-mcp"
    f = RADICE / ".mcp.json"
    cfg = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"mcpServers": {}}
    cfg.setdefault("mcpServers", {})["pedit"] = {"command": cmd, "args": [], "env": {}}
    f.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    passo("MCP server", "ok", f".mcp.json -> {cmd}")


def doctor(py: str) -> None:
    code, out = esegui([py, "-m", "pedit.cli", "doctor"])
    righe = [r for r in out.splitlines() if r.startswith("--")]
    if code:
        passo("doctor", "fail", out[-300:])
    elif righe:
        passo("doctor", "warn", "; ".join(r[3:].strip() for r in righe), "optional parts: see README, 'Optional extras'")
    else:
        passo("doctor", "ok", "everything found")


def test(py: str) -> None:
    if importlib.util.find_spec("pytest") is None and py == sys.executable:
        passo("tests", "skip", "pytest not installed")
        return
    code, out = esegui([py, "-m", "pytest", "-q", "-m", "not slow"])
    ultima = out.strip().splitlines()[-1] if out.strip() else ""
    passo("tests", "fail" if code else "ok", ultima, "run pytest -x to see the first failure" if code else "")


def main() -> int:
    global JSON
    ap = argparse.ArgumentParser(description="install pedit from this repo")
    ap.add_argument("--venv", action="store_true", help="dependencies in .venv")
    ap.add_argument("--ai", action="store_true", help="also install the ai extra (rembg, onnxruntime, opencv)")
    ap.add_argument("--no-frontend", action="store_true")
    ap.add_argument("--no-test", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--reinstall", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    JSON = a.json
    try:
        py = python_target(a.venv)
        dipendenze(py, "dev,chat,ai" if a.ai else "dev,chat", a.reinstall)
        if not a.no_frontend:
            interfaccia(a.rebuild)
        registra_mcp(py)
        doctor(py)
        if not a.no_test:
            test(py)
    except SystemExit:
        pass
    esito = "fail" if any(p["status"] == "fail" for p in PASSI) else (
        "warn" if any(p["status"] == "warn" for p in PASSI) else "ok")
    if JSON:
        print(json.dumps({"result": esito, "steps": PASSI}, indent=2))
    else:
        log(f"\nresult: {esito}" + ("  -> start with: pedit ui" if esito != "fail" else ""))
    return 1 if esito == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
