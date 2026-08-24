"""应用设置：ROOT/data/settings.json（运行时可改，gitignore）。"""
from __future__ import annotations

import json
from pathlib import Path

from ..paths import ROOT

DEFAULTS = {
    "engine": "auto",  # auto | cuda | directml
    "precision": "fp16",  # fp16（实测提速 1.36~1.73x，PSNR 74dB+）| fp32
    "download_proxy": "",  # 模型下载代理："" = 跟随系统代理 | direct = 直连 | http://host:port = 自定义
}

SETTINGS_PATH = ROOT / "data" / "settings.json"


def _valid_proxy(v: str) -> bool:
    if v in ("", "direct"):
        return True
    return v.startswith("http://") or v.startswith("https://")


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
            if k == "precision" and v not in ("fp16", "fp32"):
                raise ValueError(f"非法 precision 值: {v}")
            if k == "download_proxy" and not _valid_proxy(str(v)):
                raise ValueError(f"非法 download_proxy 值: {v}")
            data[k] = v
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data
