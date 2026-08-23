"""项目路径与捆绑二进制定位。"""
from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    """项目根目录：优先 SV_ROOT 环境变量，否则从本文件向上找含 bin/ffmpeg 的目录。"""
    env = os.environ.get("SV_ROOT")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for cand in here.parents:  # sv/ -> backend/ -> root
        if (cand / "bin" / "ffmpeg.exe").exists() or (cand / "bin" / "ffmpeg").exists():
            return cand
    return here.parents[2]


ROOT = project_root()
BIN = ROOT / "bin"
MODELS_DIR = ROOT / "models_store"
SAMPLES_DIR = ROOT / "samples"
TEMP_DIR = ROOT / ".tmp"


def ffmpeg_bin() -> str:
    env = os.environ.get("SV_FFMPEG")
    return env if env else str(BIN / "ffmpeg.exe")


def ffprobe_bin() -> str:
    env = os.environ.get("SV_FFPROBE")
    return env if env else str(BIN / "ffprobe.exe")
