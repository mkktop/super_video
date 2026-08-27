"""项目路径与捆绑二进制定位。

数据目录（DATA_ROOT）：模型/设置/缓存/TRT 组件的家。安装版由 Electron 传
SV_DATA=<安装目录同级>/super_video_data——放在安装目录**外面**，更新时
electron-builder 会先跑旧版卸载器清空安装目录，数据放里面会被连带删掉
（v0.1.20 及之前踩过的坑）。dev 无 SV_DATA 时沿用仓库目录。
"""
from __future__ import annotations

import os
import shutil
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

_env_data = os.environ.get("SV_DATA")
DATA_ROOT = Path(_env_data).resolve() if _env_data else ROOT

MODELS_DIR = DATA_ROOT / "models_store"
TEMP_DIR = DATA_ROOT / ".tmp"
SAMPLES_DIR = ROOT / "samples"
# 超分性能日志（sr_profiling 开关开启时任务结束保留）：放 data/ 下不受
# .tmp 孤儿清扫影响，应用重装/升级也不随安装目录被清
SR_LOG_DIR = DATA_ROOT / "data" / "sr_logs"

# 旧版（≤v0.1.20）放在安装目录内的数据目录：升级后一次性搬到 DATA_ROOT
_LEGACY_DIR_NAMES = ("models_store", "trt-runtime", "data", ".tmp")


def migrate_legacy_data(quiet: bool = False) -> list[str]:
    """把 ≤v0.1.20 遗留在 ROOT 下的数据目录搬到 DATA_ROOT（一次性迁移）。

    规则：源存在且目标不存在才搬（目标已有数据时不动源，不覆盖用户新数据）；
    同盘 rename 瞬时，跨盘 shutil.move 自动降级复制。返回搬走的目录名列表。
    """
    if DATA_ROOT == ROOT:
        return []
    moved: list[str] = []
    for name in _LEGACY_DIR_NAMES:
        src = ROOT / name
        dst = DATA_ROOT / name
        if not src.exists() or dst.exists():
            continue
        try:
            DATA_ROOT.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved.append(name)
        except OSError as e:  # noqa: BLE001 — 单目录失败不拦启动，下次再试
            if not quiet:
                print(f"[paths] 迁移 {name} 失败(下次启动重试): {e}", flush=True)
    if moved and not quiet:
        print(f"[paths] 已迁移旧版数据目录到 {DATA_ROOT}: {', '.join(moved)}", flush=True)
    return moved


def ffmpeg_bin() -> str:
    env = os.environ.get("SV_FFMPEG")
    return env if env else str(BIN / "ffmpeg.exe")


def ffprobe_bin() -> str:
    env = os.environ.get("SV_FFPROBE")
    return env if env else str(BIN / "ffprobe.exe")
