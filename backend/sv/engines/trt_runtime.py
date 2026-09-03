"""TRT 可选运行时组件（安装版吃上 TensorRT/CUDA 的载体）。

组件 = 数据目录里的独立文件集（ROOT/trt-runtime）：GPU 版 onnxruntime
Python 包 + CUDA/TensorRT 全部 DLL（打平进 dlls/）。安装版 sidecar 自带的
是 DML 版 onnxruntime；PyInstaller 的 FrozenImporter 挂在 sys.meta_path、
优先于一切 sys.path 查找——所以不能靠 sys.path.insert 覆盖，必须在
meta_path[0] 插入定向 finder，把 onnxruntime* 的导入重定向到组件副本。

必须在进程内首次 import onnxruntime 之前调用 activate_component()。
dev 模式不走组件：runner 直接用 .venv-cuda 解释器跑 worker（原生 GPU 版
onnxruntime，无同名包冲突）。
"""
from __future__ import annotations

import importlib.machinery
import json
import os
import sys
from pathlib import Path

from ..paths import DATA_ROOT

COMPONENT_DIR = DATA_ROOT / "trt-runtime"

# 组件须与本 sidecar 的 Python ABI 一致（pyd 是按版本编译的）
EXPECTED_ABI = f"cp{sys.version_info.major}{sys.version_info.minor}"


class _ComponentFinder:
    """只接管 onnxruntime* 前缀的导入，指向组件里的 python/ 目录。"""

    def __init__(self, root: Path) -> None:
        self._root = [str(root)]

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
        if fullname == "onnxruntime" or fullname.startswith("onnxruntime."):
            return importlib.machinery.PathFinder.find_spec(fullname, self._root)
        return None


def read_manifest(comp: Path | None = None) -> dict | None:
    p = (comp or COMPONENT_DIR) / "manifest.json"
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # ValueError 兼盖 JSON/UTF-8 解码错误
        return None
    if not isinstance(m, dict) or m.get("component") != "trt":
        return None
    return m


def find_component() -> Path | None:
    """已安装且与本 sidecar 兼容的组件目录；无/不兼容返回 None。"""
    if not COMPONENT_DIR.is_dir():
        return None
    m = read_manifest()
    if m is None or m.get("python") != EXPECTED_ABI:
        return None
    if not (COMPONENT_DIR / "python" / "onnxruntime").is_dir():
        return None
    if not (COMPONENT_DIR / "dlls").is_dir():
        return None
    return COMPONENT_DIR


_activated = False


def activate_component() -> bool:
    """注册组件 DLL 路径并把 onnxruntime 导入重定向到组件副本。

    重复调用无害；返回 False = 组件缺失/不兼容/激活过晚，调用方按无 TRT
    处理（引擎层照常回退 DML）。仅限 frozen（打包版）进程使用。
    """
    global _activated
    if _activated:
        return True
    comp = find_component()
    if comp is None:
        return False
    if "onnxruntime" in sys.modules:
        # 进程里已 import 过 DML 版（bundled），同名包重定向已不可能
        print("[trt-component] onnxruntime 已加载，组件激活过晚", file=sys.stderr, flush=True)
        return False
    dlls = comp / "dlls"
    try:
        os.add_dll_directory(str(dlls))
    except OSError:
        pass
    os.environ["PATH"] = str(dlls) + os.pathsep + os.environ.get("PATH", "")
    sys.meta_path.insert(0, _ComponentFinder(comp / "python"))
    _activated = True
    return True
