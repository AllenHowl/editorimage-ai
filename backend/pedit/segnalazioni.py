"""记录智能体工作中发现的问题。

``report_issue`` 向 ``SEGNALAZIONI.md`` 追加条目。源码运行时位于仓库根目录，
安装包运行时位于 ``~/.pedit/SEGNALAZIONI.md``；可用 ``PEDIT_SEGNALAZIONI``
指定其他位置。此文件是后续改进的依据。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

TIPI = ("bug", "scomodo", "sbagliato", "manca", "lento")


def file_segnalazioni() -> Path:
    env = os.environ.get("PEDIT_SEGNALAZIONI")
    if env:
        return Path(env)
    radice = Path(__file__).resolve().parents[2]
    if (radice / "pyproject.toml").is_file() and (radice / "backend" / "pedit").is_dir():
        return radice / "SEGNALAZIONI.md"
    base = Path(os.environ.get("PEDIT_HOME") or (Path.home() / ".pedit"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "SEGNALAZIONI.md"


def aggiungi(tipo: str, titolo: str, dettagli: str = "", strumento: str = "") -> dict:
    if tipo not in TIPI:
        raise ValueError(f"kind 必须为以下选项之一：{', '.join(TIPI)}")
    if not titolo.strip():
        raise ValueError("标题不能为空")
    f = file_segnalazioni()
    nuovo = not f.exists()
    righe = []
    if nuovo:
        righe.append("# 问题记录\n\n记录智能体发现的故障、使用不便和缺失功能。"
                     "问题修复后请删除对应条目。\n")
    quando = time.strftime("%Y-%m-%d %H:%M")
    righe.append(f"\n## [{tipo}] {titolo.strip()}\n\n- 时间：{quando}\n"
                 + (f"- 工具：`{strumento}`\n" if strumento else "")
                 + (f"\n{dettagli.strip()}\n" if dettagli.strip() else ""))
    with f.open("a", encoding="utf-8") as fh:
        fh.write("".join(righe))
    return {"saved": str(f)}
