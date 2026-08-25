"""把 pip 安装的 NVIDIA 运行库 DLL 挂到搜索路径。

onnxruntime-gpu 的 CUDA EP 需要 cudart/cublas/cudnn 的 DLL；pip 安装的
nvidia-*-cu12 包把 DLL 放在 site-packages/nvidia/<lib>/bin，默认不在搜索
路径里。在任何进程创建 ORT 会话前调用本函数（重复调用无害）。
"""
from __future__ import annotations

import os
import sysconfig
from pathlib import Path


def register_dir(d: Path) -> None:
    """把一个目录挂进 DLL 搜索路径（add_dll_directory + PATH 前插）。"""
    try:
        os.add_dll_directory(str(d))
    except OSError:
        pass
    os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


def register_nvidia_dlls() -> None:
    site = Path(sysconfig.get_paths()["purelib"])

    _register = register_dir

    nv = site / "nvidia"
    if nv.is_dir():
        for bin_dir in sorted(nv.glob("*/bin")):
            if bin_dir.is_dir():
                _register(bin_dir)
    # TensorRT 运行库（pip tensorrt-cu12-libs）：DLL 直接躺在 tensorrt_libs/ 下，
    # ORT 的 TRT 后端要找 nvinfer_10.dll，不挂路径会静默回退 CUDA
    trt = site / "tensorrt_libs"
    if trt.is_dir():
        _register(trt)
