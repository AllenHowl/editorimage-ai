"""共用测试夹具：合成图片及临时目录中的文档。

不访问网络或真实模型；``PEDIT_HOME`` 指向临时目录，
避免测试修改运行者的 ``~/.pedit`` 配置。
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from pedit.store import Store


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("PEDIT_HOME", str(tmp_path / "home"))
    # 使用关闭的端口，模拟未运行的 ComfyUI。
    monkeypatch.setenv("PEDIT_COMFY_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("PEDIT_SEGNALAZIONI", str(tmp_path / "SEGNALAZIONI.md"))


@pytest.fixture
def photo(tmp_path):
    """合成照片：水平渐变，中间有一个黄色方块。"""
    h, w = 120, 240
    arr = np.zeros((h, w, 3), np.uint8)
    arr[..., 0] = np.linspace(0, 255, w)[None, :]
    arr[..., 2] = 120
    arr[40:80, 100:140] = [250, 230, 20]
    p = tmp_path / "photo.png"
    Image.fromarray(arr).save(p)
    return str(p)


@pytest.fixture
def store(tmp_path):
    return Store.create(name="t", width=400, height=200, background="#000000", path=str(tmp_path / "doc.json"))


def px(img, x, y):
    return img.convert("RGBA").getpixel((x, y))
