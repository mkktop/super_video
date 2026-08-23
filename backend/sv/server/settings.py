"""应用设置：ROOT/data/settings.json（运行时可改，gitignore）。"""
from __future__ import annotations

import json
from pathlib import Path

from ..paths import ROOT

DEFAULTS = {
    "engine": "auto",  # auto | cuda | directml
}

SETTINGS_PATH = ROOT / "data" / "settings.json"


def load() -> dict:
    data = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return data


def save(updates: dict) -> dict:
    data = load()
    for k in DEFAULTS:
        if k in updates:
            v = updates[k]
            if k == "engine" and v not in ("auto", "cuda", "directml"):
                raise ValueError(f"非法 engine 值: {v}")
            data[k] = v
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data
