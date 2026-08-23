"""推理后端选择：优先 CUDA（独立 venv），回落 DirectML/CPU（主 venv）。

worker 是独立子进程，用哪个解释器启动就用哪套 onnxruntime —— 以此规避
onnxruntime-gpu 与 onnxruntime-directml 的同名包冲突。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..engines.nvidia_dlls import register_nvidia_dlls
from ..models.registry import BUNDLED_DIR
from ..paths import ROOT
from ..utils.process import WINDOWS_CREATE_FLAGS

BACKEND_DIR = Path(__file__).resolve().parents[2]


@dataclass
class EngineChoice:
    backend: str  # cuda | directml
    python_exe: str
    detail: str = ""


def _cuda_venv_python() -> Path | None:
    p = ROOT / ".venv-cuda" / "Scripts" / "python.exe"
    return p if p.exists() else None


def _probe_cuda(python_exe: Path) -> tuple[bool, str]:
    """在候选解释器里跑真会话验证；返回 (ok, 说明)。"""
    model = BUNDLED_DIR / "RealESR-AnimeVideo-v3_x4.onnx"
    if not model.exists():
        return False, "缺少校验用模型"
    script = BACKEND_DIR / "scripts" / "check_cuda.py"
    try:
        out = subprocess.run(
            [str(python_exe), str(script), str(model)],
            capture_output=True, timeout=120,
            creationflags=WINDOWS_CREATE_FLAGS,
        )
        line = out.stdout.decode("utf-8", "replace").strip().splitlines()[-1]
        info = json.loads(line)
        return bool(info.get("ok")), info.get("error") or info.get("provider", "")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, IndexError) as e:
        return False, f"探测失败: {e}"


def select_engine(force: str | None = None) -> EngineChoice:
    """决定 worker 用哪个解释器。

    实测策略（2026-08-23, RTX 5080, 见 BENCH.md）：当前模型库（小模型/小tile高频调用）
    DirectML 全面快于 CUDA EP（animevideov3 +10~25%, x4plus +270%），故默认 DirectML；
    CUDA 基础设施保留，SV_ENGINE=cuda 强制启用（M3 扩散模型 PyTorch 阶段启用）。
    force: 'cuda' | 'directml'（环境变量 SV_ENGINE）。
    """
    force = force or os.environ.get("SV_ENGINE")
    if force == "cuda":
        py = _cuda_venv_python()
        if py is not None:
            ok, detail = _probe_cuda(py)
            if ok:
                return EngineChoice("cuda", str(py), detail)
        return EngineChoice(
            "directml", sys.executable, "SV_ENGINE=cuda 但 CUDA 不可用，回落 DirectML"
        )
    register_nvidia_dlls()
    return EngineChoice("directml", sys.executable)
