"""TRT 可选运行时组件：导入重定向 / 兼容性判据 / 安装状态机守卫。

承重机制（frozen exe 真会话已在 BENCH.md 实测验证）此处单测覆盖判据分支：
组件目录合法性、ABI 不兼容拒绝、激活过晚拒绝、finder 只接管 onnxruntime*。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

import sv.engines.trt_runtime as trt
import sv.server.trt_component as tc


def _make_component(root: Path, abi: str | None = "cp314") -> Path:
    """组装一个最小合法组件目录（python/onnxruntime + dlls + manifest）。"""
    (root / "python" / "onnxruntime").mkdir(parents=True, exist_ok=True)
    (root / "python" / "onnxruntime" / "__init__.py").write_text("")
    (root / "dlls").mkdir(exist_ok=True)
    (root / "dlls" / "nvinfer_10.dll").write_bytes(b"\0" * 8)
    m = {"component": "trt", "version": 1, "python": abi or "", "ort": "x", "trt": "y"}
    (root / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    return root


# ---- trt_runtime ----

def test_find_component_ok(tmp_path, monkeypatch):
    comp = _make_component(tmp_path / "trt-runtime")
    monkeypatch.setattr(trt, "COMPONENT_DIR", comp)
    assert trt.find_component() == comp


def test_find_component_rejects(tmp_path, monkeypatch):
    base = tmp_path
    monkeypatch.setattr(trt, "COMPONENT_DIR", base / "trt-runtime")
    # 目录不存在
    assert trt.find_component() is None
    # manifest 缺失（目录空）
    comp = _make_component(base / "trt-runtime")
    (comp / "manifest.json").unlink()
    assert trt.find_component() is None
    # ABI 不匹配
    _make_component(comp, abi="cp39")
    assert trt.find_component() is None
    # 缺 python/onnxruntime
    _make_component(comp)
    shutil.rmtree(comp / "python")
    assert trt.find_component() is None
    # manifest 非法 JSON
    comp2 = _make_component(base / "trt-runtime2")
    monkeypatch.setattr(trt, "COMPONENT_DIR", comp2)
    (comp2 / "manifest.json").write_text("{broken", encoding="utf-8")
    assert trt.find_component() is None
    assert trt.read_manifest() is None


def test_activate_late_or_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(trt, "COMPONENT_DIR", tmp_path / "none")
    assert trt.activate_component() is False  # 组件缺失
    # 已 import 过 onnxruntime 的进程：重定向已不可能，必须拒绝
    _make_component(tmp_path / "trt-runtime")
    monkeypatch.setattr(trt, "COMPONENT_DIR", tmp_path / "trt-runtime")
    fake = object()
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    assert trt.activate_component() is False


def test_component_finder_only_takes_onnxruntime(tmp_path):
    finder = trt._ComponentFinder(tmp_path)
    # 非 onnxruntime 前缀直接谢绝（不进 PathFinder）
    assert finder.find_spec("numpy") is None
    assert finder.find_spec("onnxruntime_foo") is None
    pkg = tmp_path / "onnxruntime"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    spec = finder.find_spec("onnxruntime")
    assert spec is not None and spec.origin is not None
    assert str(tmp_path) in spec.origin


# ---- trt_component（状态/守卫） ----

def test_detect_gpu_arch_format():
    arch = tc.detect_gpu_arch()
    assert arch is None or (isinstance(arch, str) and arch.startswith("sm"))


def test_status_with_fake_component(tmp_path, monkeypatch):
    comp = _make_component(tmp_path / "trt-runtime")
    # 两个模块各持一份 COMPONENT_DIR 副本（Path 不可变），都要替换才不读真目录
    monkeypatch.setattr(tc, "COMPONENT_DIR", comp)
    monkeypatch.setattr(trt, "COMPONENT_DIR", comp)
    st = tc.status()
    assert st["installed"] is True
    assert st["version"] == 1
    expected = sum(f.stat().st_size for f in comp.rglob("*") if f.is_file())
    assert st["size_bytes"] == expected
    assert st["installing"] is False


def test_start_install_guard_busy(monkeypatch):
    # busy 检查在 runner 导入之前，可直接验证拒绝分支
    monkeypatch.setattr(tc, "_state", {"installing": True, "phase": "download",
                                       "file": "", "done": 0, "total": 0, "error": None})
    ok, msg = tc.start_install(bus=None)
    assert ok is False
    assert "正在安装" in msg


def test_uninstall_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "COMPONENT_DIR", tmp_path / "none")
    ok, msg = tc.uninstall()
    assert ok is True and msg == ""


def test_load_assets_invalid(tmp_path, monkeypatch):
    import sv.server.trt_component as mod

    monkeypatch.setattr(mod, "_ASSET_FILE", tmp_path / "nope.json")
    assert mod.load_assets() == {}
    (tmp_path / "nope.json").write_text("{bad", encoding="utf-8")
    assert mod.load_assets() == {}
