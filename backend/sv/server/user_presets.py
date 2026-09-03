"""用户自定义预设：DATA_ROOT/data/user_presets.json（原子写，条数上界淘汰最旧）。

内置预设（presets.json）随包只读；用户在新建任务页把当前参数保存为
"我的预设"，字段与内置同构（model/target_scale/codec/crf + 可选处理项），
前端统一渲染成预设条里的按钮。存 JSON 文件而非任务库：与队列无关的
纯配置数据，与 settings.json 同目录同写法（tmp + 原子换名）。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from ..paths import DATA_ROOT

PRESETS_PATH = DATA_ROOT / "data" / "user_presets.json"
MAX_PRESETS = 50  # 超出淘汰最旧（保存门槛极低，上界只防无界增长）


def load() -> list[dict]:
    if not PRESETS_PATH.exists():
        return []
    try:
        data = json.loads(PRESETS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # ValueError 兼盖 UnicodeDecodeError（文件被外部存成 GBK 等）
        return []  # 损坏按无预设处理，不阻塞内置预设展示
    return [p for p in data if isinstance(p, dict) and p.get("id")]


def _write(presets: list[dict]) -> None:
    PRESETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PRESETS_PATH.with_name(PRESETS_PATH.name + f".tmp{uuid.uuid4().hex[:6]}")
    tmp.write_text(json.dumps(presets, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PRESETS_PATH)


def add(preset: dict) -> dict:
    """落盘一条用户预设（调用方已完成字段校验），返回带 id 的完整记录。"""
    rec = {
        "id": f"user-{uuid.uuid4().hex[:8]}",
        "name": preset["name"],
        "icon": preset.get("icon") or "⭐",
        "desc": preset.get("desc") or "",
        "user": True,
        "created_at": time.time(),
        **{k: v for k, v in preset.items()
           if k in ("model_id", "target_scale", "codec", "crf", "container",
                    "audio_mode", "subtitle_mode", "interp", "denoise",
                    "deinterlace", "deband")},
    }
    presets = load() + [rec]
    _write(presets[-MAX_PRESETS:])
    return rec


def remove(preset_id: str) -> bool:
    presets = load()
    rest = [p for p in presets if p.get("id") != preset_id]
    if len(rest) == len(presets):
        return False
    _write(rest)
    return True
