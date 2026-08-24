"""硬件检测：GPU / 显存 / CPU / 内存。"""
from __future__ import annotations

import platform
import re
import subprocess

import psutil

from ..utils.process import WINDOWS_CREATE_FLAGS


def _gpu_names() -> list[str]:
    gpus: list[str] = []
    # 优先 nvidia-smi（N 卡最常见）
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, timeout=8, creationflags=WINDOWS_CREATE_FLAGS,
        )
        if out.returncode == 0:
            for line in out.stdout.decode("utf-8", "replace").strip().splitlines():
                name, _, vram = line.partition(",")
                gpus.append({
                    "name": name.strip(),
                    "vram_gb": round(float(re.sub(r"[^\d.]", "", vram or "0")) / 1024, 1),
                })
            return gpus
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    # 兜底 CIM
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
            capture_output=True, timeout=15, creationflags=WINDOWS_CREATE_FLAGS,
        )
        if out.returncode == 0:
            for line in out.stdout.decode("utf-8", "replace").strip().splitlines():
                if line.strip():
                    gpus.append({"name": line.strip(), "vram_gb": None})
    except (OSError, subprocess.TimeoutExpired):
        pass
    return gpus


def _cpu_name() -> str:
    """真实 CPU 型号（platform.processor() 在 Windows 返回原始串，不可读）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Processor).Name"],
            capture_output=True, timeout=15, creationflags=WINDOWS_CREATE_FLAGS,
        )
        if out.returncode == 0:
            name = out.stdout.decode("utf-8", "replace").strip()
            if name:
                return name
    except (OSError, subprocess.TimeoutExpired):
        pass
    return platform.processor() or platform.machine()


def _try_encode(encoder: str, min_w: int = 320) -> bool:
    """试编码一帧验证硬编码器可用（驱动/会话正常才算数）。

    分辨率须 ≥ 硬编最小宽度（NVENC H.264 为 145），否则参数非法误报不可用。
    """
    from ..paths import ffmpeg_bin

    try:
        out = subprocess.run(
            [ffmpeg_bin(), "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", f"color=c=black:s={min_w}x240:d=0.2",
             "-c:v", encoder, "-f", "null", "-"],
            capture_output=True, timeout=20, creationflags=WINDOWS_CREATE_FLAGS,
        )
        return out.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _has_encoder(name: str) -> bool:
    """ffmpeg 构建是否带某软编（如 libsvtav1），纯列表查询不试编码。"""
    from ..paths import ffmpeg_bin

    try:
        out = subprocess.run(
            [ffmpeg_bin(), "-hide_banner", "-encoders"],
            capture_output=True, timeout=15, creationflags=WINDOWS_CREATE_FLAGS,
        )
        return name in out.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return False


def hardware_info() -> dict:
    vm = psutil.virtual_memory()
    return {
        "gpus": _gpu_names(),
        "cpu": _cpu_name(),
        "cpu_cores": psutil.cpu_count(logical=False),
        "ram_gb": round(vm.total / 1e9, 1),
        "nvenc": _try_encode("h264_nvenc"),
        "av1_nvenc": _try_encode("av1_nvenc"),
        "amf": _try_encode("h264_amf"),
        "svt_av1": _has_encoder("libsvtav1"),
    }
