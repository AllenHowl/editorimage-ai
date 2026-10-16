"""Look gia' tarati: una catena di filtri con un nome al posto di venti parametri.

Un look non e' una scatola chiusa: applicato diventa effetti normali (o un
livello di regolazione), quindi ogni parametro resta modificabile e un Ctrl+Z
lo toglie tutto.
"""

from __future__ import annotations

LOOKS: dict[str, dict] = {
    "cinematic": {
        "desc": "Teal shadows, warm highlights, gentle contrast",
        "effects": [
            {"type": "curves", "params": {"rgb": [[0, 12], [64, 58], [192, 200], [255, 248]],
                                          "b": [[0, 30], [128, 128], [255, 225]],
                                          "r": [[0, 0], [128, 130], [255, 255]]}},
            {"type": "saturation", "params": {"amount": -0.1}},
            {"type": "vignette", "params": {"amount": 0.35, "softness": 0.6}},
        ]},
    "black_white": {
        "desc": "Contrasty black and white",
        "effects": [{"type": "grayscale", "params": {"amount": 1.0}},
                    {"type": "contrast", "params": {"amount": 0.25}}]},
    "warm": {"desc": "Golden hour warmth",
             "effects": [{"type": "temperature", "params": {"amount": 0.45}},
                         {"type": "vibrance", "params": {"amount": 0.2}}]},
    "cool": {"desc": "Cold, clean blue cast",
             "effects": [{"type": "temperature", "params": {"amount": -0.4}},
                         {"type": "contrast", "params": {"amount": 0.1}}]},
    "faded_film": {
        "desc": "Lifted blacks, soft contrast, grain",
        "effects": [{"type": "levels", "params": {"out_black": 28, "out_white": 238}},
                    {"type": "saturation", "params": {"amount": -0.2}},
                    {"type": "noise", "params": {"amount": 0.12, "mono": True}}]},
    "vivid": {"desc": "Punchy colors for social",
              "effects": [{"type": "vibrance", "params": {"amount": 0.5}},
                          {"type": "contrast", "params": {"amount": 0.2}},
                          {"type": "sharpen", "params": {"amount": 0.5}}]},
    "matte": {"desc": "Flat matte with muted colors",
              "effects": [{"type": "curves", "params": {"rgb": [[0, 35], [128, 128], [255, 230]]}},
                          {"type": "saturation", "params": {"amount": -0.25}}]},
    "vintage": {"desc": "Sepia-tinted, faded and vignetted",
                "effects": [{"type": "sepia", "params": {"amount": 0.55}},
                            {"type": "levels", "params": {"out_black": 20}},
                            {"type": "vignette", "params": {"amount": 0.5}},
                            {"type": "noise", "params": {"amount": 0.1}}]},
    "dramatic": {"desc": "Deep shadows, strong local contrast",
                 "effects": [{"type": "contrast", "params": {"amount": 0.45}},
                             {"type": "shadows_highlights", "params": {"shadows": -0.2, "highlights": 0.4}},
                             {"type": "saturation", "params": {"amount": -0.15}},
                             {"type": "vignette", "params": {"amount": 0.55}}]},
    "clean_portrait": {"desc": "Soft skin-friendly brightening",
                       "effects": [{"type": "shadows_highlights", "params": {"shadows": 0.25}},
                                   {"type": "vibrance", "params": {"amount": 0.15}},
                                   {"type": "temperature", "params": {"amount": 0.1}}]},
}


def describe() -> list[dict]:
    return [{"name": k, "desc": v["desc"], "effects": [e["type"] for e in v["effects"]]}
            for k, v in LOOKS.items()]
