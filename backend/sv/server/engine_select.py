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


_PROBE_CACHE: dict[str, tuple[bool, str]] = {}


def _probe_cuda(python_exe: Path) -> tuple[bool, str]:
    """在候选解释器里跑真会话验证；返回 (ok, 说明)。结果缓存（进程生命周期内）。"""
    key = str(python_exe)
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    model = BUNDLED_DIR / "RealESR-AnimeVideo-v3_x4.onnx"
    if not model.exists():
        return False, "缺少校验用模型"
    script = BACKEND_DIR / "scripts" / "check_cuda.py"
    try:
        out = subprocess.run(
            [str(python_exe), str(script), str(model)],
            capture_output=True, timeout=120,
            creationflags=WINDOWS_CREATE_FLAGS,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
        line = out.stdout.decode("utf-8", "replace").strip().splitlines()[-1]
        info = json.loads(line)
        result = (bool(info.get("ok")), info.get("error") or info.get("provider", ""))
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, IndexError) as e:
        result = (False, f"探测失败: {e}")
    _PROBE_CACHE[key] = result
    return result


def _probe_frozen() -> tuple[bool, str]:
    """安装版组件探测：sidecar.exe ort-check --session（组件 CUDA 链真会话）。"""
    key = f"frozen:{sys.executable}"
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    model = BUNDLED_DIR / "RealESR-AnimeVideo-v3_x4.onnx"
    if not model.exists():
        return False, "缺少校验用模型"
    try:
        out = subprocess.run(
            [sys.executable, "ort-check", "--session", str(model)],
            capture_output=True, timeout=180,
            creationflags=WINDOWS_CREATE_FLAGS,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
        line = out.stdout.decode("utf-8", "replace").strip().splitlines()[-1]
        info = json.loads(line)
        detail = ("组件 CUDA" + (" + TensorRT" if info.get("trt") else "")
                  if info.get("ok") else (info.get("error") or "探测失败"))
        result = (bool(info.get("ok")), detail)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, IndexError) as e:
        result = (False, f"探测失败: {e}")
    _PROBE_CACHE[key] = result
    return result


def select_engine(force: str | None = None) -> EngineChoice:
    """决定 worker 用哪个解释器。

    实测策略（2026-08-23, RTX 5080, 见 BENCH.md）：当前模型库（小模型/小tile高频调用）
    DirectML 全面快于 CUDA EP（animevideov3 +10~25%, x4plus +270%），故默认 DirectML；
    CUDA 基础设施保留，SV_ENGINE=cuda 强制启用（M3 扩散模型 PyTorch 阶段启用）。
    trt = CUDA venv + TensorrtExecutionProvider（worker 内按同名设置再选 provider；
    TRT 库缺失时引擎层自动回退，此处只保证解释器可用）。
    cpu = 主解释器 + CPUExecutionProvider（GPU 问题兜底：CUGAN×DML 泄漏模型的
    报错文案即引导用户显式切 CPU，设置白名单同步允许）。
    force: 'cuda' | 'trt' | 'directml' | 'cpu'（环境变量 SV_ENGINE）。

    打包版（PyInstaller frozen）：worker 复用 sidecar.exe；engine=trt/cuda 且
    TRT 组件已安装（trt-runtime/，含 GPU 版 onnxruntime）时走组件探测，
    否则照旧 DirectML。
    """
    if force == "cpu" or os.environ.get("SV_ENGINE") == "cpu":
        return EngineChoice(
            "cpu", sys.executable, "纯 CPU 推理（速度慢，GPU 问题时的兜底通道）")
    if getattr(sys, "frozen", False):
        want = force or os.environ.get("SV_ENGINE")
        if want in ("cuda", "trt"):
            from ..engines.trt_runtime import find_component

            if find_component() is not None:
                ok, detail = _probe_frozen()
                if ok:
                    return EngineChoice(want, sys.executable, detail)
                return EngineChoice("directml", sys.executable,
                                    f"组件探测失败，回落 DirectML（{detail}）")
            return EngineChoice("directml", sys.executable,
                                "TRT 组件未安装，回落 DirectML")
        return EngineChoice("directml", sys.executable, "")
    force = force or os.environ.get("SV_ENGINE")
    if force in ("cuda", "trt"):
        py = _cuda_venv_python()
        if py is not None:
            ok, detail = _probe_cuda(py)
            if ok:
                backend = "trt" if force == "trt" else "cuda"
                return EngineChoice(backend, str(py), detail)
        return EngineChoice(
            "directml", sys.executable, "CUDA 不可用，回落 DirectML"
        )
    register_nvidia_dlls()
    return EngineChoice("directml", sys.executable)
