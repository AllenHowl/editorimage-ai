"""构建用于发布到 PyPI 的安装包。

在 ``python -m build`` 之前，必须把编译后的网页界面复制到包内。
PyPI 用户不需要安装 Node；如果缺少界面资源，``pedit ui`` 和
``open_ui`` 会打开空白页面。

    python scripts/build_wheel.py            # 编译界面并构建安装包
    python scripts/build_wheel.py --no-ui    # 复用现有 frontend/dist

输出 ``dist/pedit_mcp-*.whl`` 和 ``.tar.gz``。手动发布可运行
``python -m twine upload dist/*``，通常由 GitHub Actions 在打标签后处理。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
DIST_UI = RADICE / "frontend" / "dist"
DENTRO = RADICE / "backend" / "pedit" / "webui"
DIST = RADICE / "dist"


def esegui(cmd: list[str], cwd: Path) -> None:
    print(f"  $ {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=cwd, shell=(sys.platform == "win32"))
    if proc.returncode != 0:
        raise SystemExit(f"fallito: {' '.join(cmd)}")


def compila_ui() -> None:
    front = RADICE / "frontend"
    if not (front / "node_modules").is_dir():
        esegui(["npm", "ci"], front)
    esegui(["npm", "run", "build"], front)


def copia_ui() -> None:
    if not (DIST_UI / "index.html").is_file():
        raise SystemExit(
            f"manca {DIST_UI}/index.html: compila l'interfaccia prima "
            "(`cd frontend && npm run build`) o togli --no-ui"
        )
    shutil.rmtree(DENTRO, ignore_errors=True)
    shutil.copytree(DIST_UI, DENTRO)
    peso = sum(f.stat().st_size for f in DENTRO.rglob("*") if f.is_file())
    print(f"  interfaccia copiata in {DENTRO} ({peso / 1e6:.1f} MB)")


def main() -> int:
    ap = argparse.ArgumentParser(description="costruisce il pacchetto per PyPI")
    ap.add_argument("--no-ui", action="store_true", help="non ricompilare la UI, usa frontend/dist")
    ap.add_argument("--keep", action="store_true", help="lascia backend/pedit/webui dopo la build")
    a = ap.parse_args()

    print("1. interfaccia web")
    if not a.no_ui:
        compila_ui()
    copia_ui()

    print("2. pacchetto")
    shutil.rmtree(DIST, ignore_errors=True)
    esegui([sys.executable, "-m", "build"], RADICE)

    if not a.keep:
        # 开发时包内界面副本会优先于 frontend/dist，
        # 导致 npm run build 看起来没有生效，因此完成后移除副本。
        shutil.rmtree(DENTRO, ignore_errors=True)

    print("\nfatto:")
    for f in sorted(DIST.glob("*")):
        print(f"  {f.name}  ({f.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
