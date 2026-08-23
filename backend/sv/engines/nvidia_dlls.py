"""把 pip 安装的 NVIDIA 运行库 DLL 挂到搜索路径。

onnxruntime-gpu 的 CUDA EP 需要 cudart/cublas/cudnn 的 DLL；pip 安装的
nvidia-*-cu12 包把 DLL 放在 site-packages/nvidia/<lib>/bin，默认不在搜索
路径里。在任何进程创建 ORT 会话前调用本函数（重复调用无害）。
"""
from __future__ import annotations

import os
import sysconfig
from pathlib import Path


def register_nvidia_dlls() -> None:
    site = Path(sysconfig.get_paths()["purelib"])
    nv = site / "nvidia"
    if not nv.exists():
        return
    for bin_dir in sorted(nv.glob("*/bin")):
        if bin_dir.is_dir():
            try:
                os.add_dll_directory(str(bin_dir))
            except OSError:
                pass
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
