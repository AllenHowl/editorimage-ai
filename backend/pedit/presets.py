"""预设风格：用一个名称封装预先调好的滤镜链。

应用后仍是普通效果或调整图层，各项参数均可编辑，按 Ctrl+Z 可整体撤销。
"""

from __future__ import annotations

LOOKS: dict[str, dict] = {
    "cinematic": {"label": "电影感",
        "desc": "青色阴影、暖色高光与柔和对比度",
        "effects": [
            {"type": "curves", "params": {"rgb": [[0, 12], [64, 58], [192, 200], [255, 248]],
                                          "b": [[0, 30], [128, 128], [255, 225]],
                                          "r": [[0, 0], [128, 130], [255, 255]]}},
            {"type": "saturation", "params": {"amount": -0.1}},
            {"type": "vignette", "params": {"amount": 0.35, "softness": 0.6}},
        ]},
    "black_white": {"label": "经典黑白",
        "desc": "高对比度黑白效果",
        "effects": [{"type": "grayscale", "params": {"amount": 1.0}},
                    {"type": "contrast", "params": {"amount": 0.25}}]},
    "warm": {"label": "暖色", "desc": "金色时刻的暖意",
             "effects": [{"type": "temperature", "params": {"amount": 0.45}},
                         {"type": "vibrance", "params": {"amount": 0.2}}]},
    "cool": {"label": "冷色", "desc": "清透的冷蓝色调",
             "effects": [{"type": "temperature", "params": {"amount": -0.4}},
                         {"type": "contrast", "params": {"amount": 0.1}}]},
    "faded_film": {"label": "褪色胶片",
        "desc": "提亮黑场，柔化对比度，增加颗粒感",
        "effects": [{"type": "levels", "params": {"out_black": 28, "out_white": 238}},
                    {"type": "saturation", "params": {"amount": -0.2}},
                    {"type": "noise", "params": {"amount": 0.12, "mono": True}}]},
    "vivid": {"label": "鲜艳", "desc": "适合社交媒体的鲜艳色彩",
              "effects": [{"type": "vibrance", "params": {"amount": 0.5}},
                          {"type": "contrast", "params": {"amount": 0.2}},
                          {"type": "sharpen", "params": {"amount": 0.5}}]},
    "matte": {"label": "哑光", "desc": "低饱和度的柔和哑光效果",
              "effects": [{"type": "curves", "params": {"rgb": [[0, 35], [128, 128], [255, 230]]}},
                          {"type": "saturation", "params": {"amount": -0.25}}]},
    "vintage": {"label": "复古", "desc": "褐色调、褪色与暗角",
                "effects": [{"type": "sepia", "params": {"amount": 0.55}},
                            {"type": "levels", "params": {"out_black": 20}},
                            {"type": "vignette", "params": {"amount": 0.5}},
                            {"type": "noise", "params": {"amount": 0.1}}]},
    "dramatic": {"label": "戏剧感", "desc": "深邃阴影与强烈局部对比",
                 "effects": [{"type": "contrast", "params": {"amount": 0.45}},
                             {"type": "shadows_highlights", "params": {"shadows": -0.2, "highlights": 0.4}},
                             {"type": "saturation", "params": {"amount": -0.15}},
                             {"type": "vignette", "params": {"amount": 0.55}}]},
    "clean_portrait": {"label": "清透人像", "desc": "柔和提亮，适合人像肤色",
                       "effects": [{"type": "shadows_highlights", "params": {"shadows": 0.25}},
                                   {"type": "vibrance", "params": {"amount": 0.15}},
                                   {"type": "temperature", "params": {"amount": 0.1}}]},
}


def describe() -> list[dict]:
    return [{"name": k, "label": v["label"], "desc": v["desc"], "effects": [e["type"] for e in v["effects"]]}
            for k, v in LOOKS.items()]
