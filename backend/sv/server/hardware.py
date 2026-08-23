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


def hardware_info() -> dict:
    vm = psutil.virtual_memory()
    return {
        "gpus": _gpu_names(),
        "cpu": _cpu_name(),
        "cpu_cores": psutil.cpu_count(logical=False),
        "ram_gb": round(vm.total / 1e9, 1),
    }
