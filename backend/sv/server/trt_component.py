"""TRT 可选运行时组件的安装/卸载/状态（数据目录 trt-runtime/ 的管家）。

资产 = GitHub release（tag runtime-v1）上的 7z 分包：
- core：GPU 版 onnxruntime 包 + CUDA 运行库 + TRT 核心库（必装）
- builder-smXX / builder-ptx：TRT 引擎编译资源，按显卡架构选装
  （nvidia-smi compute_cap 12.0 → sm120；探测失败/无匹配时装 ptx 通配包）

下载/校验复用模型下载器（流式+sha256+代理设置）；解压用 py7zr（随包捆绑）。
进度经 EventBus 广播 {"type":"trt_component", ...}，设置页卡片消费。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from pathlib import Path

from ..engines.trt_runtime import COMPONENT_DIR, read_manifest
from ..paths import ROOT
from ..utils.process import WINDOWS_CREATE_FLAGS
from .events import EventBus

_ASSET_FILE = Path(__file__).parent / "trt_component_assets.json"
_STAGING = ROOT / "trt-runtime.new"
_OLD = ROOT / "trt-runtime.old"

_state_lock = threading.Lock()
_state: dict = {
    "installing": False,
    "phase": None,          # download | extract | done | error
    "file": "",
    "done": 0,              # 已下载/已解压字节
    "total": 0,
    "error": None,
}
_install_thread: threading.Thread | None = None


def load_assets() -> dict:
    try:
        data = json.loads(_ASSET_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def detect_gpu_arch() -> str | None:
    """N 卡计算能力 → builder 资产键（12.0→sm120）；非 N 卡/失败返回 None。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
            capture_output=True, timeout=8, creationflags=WINDOWS_CREATE_FLAGS,
        )
        if out.returncode != 0:
            return None
        first = out.stdout.decode("utf-8", "replace").strip().splitlines()[0]
        m = re.match(r"(\d+)\.(\d+)", first.strip())
        if not m:
            return None
        return f"sm{m.group(1)}{m.group(2)}"
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


def component_size(comp: Path | None = None) -> int:
    d = comp or COMPONENT_DIR
    if not d.is_dir():
        return 0
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


def status() -> dict:
    # 显式传本模块的 COMPONENT_DIR：read_manifest 的默认参读的是 trt_runtime
    # 模块自己的同名属性，测试替换本模块副本时两者会分叉（CI 无本地组件目录时暴露）
    manifest = read_manifest(COMPONENT_DIR)
    with _state_lock:
        st = dict(_state)
    return {
        "installed": manifest is not None,
        "version": (manifest or {}).get("version"),
        "ort": (manifest or {}).get("ort"),
        "trt": (manifest or {}).get("trt"),
        "python": (manifest or {}).get("python"),
        "size_bytes": component_size() if manifest is not None else 0,
        "gpu_arch": detect_gpu_arch(),
        "assets": load_assets(),
        **{k: st[k] for k in ("installing", "phase", "file", "done", "total", "error")},
    }


# ---- 下载/解压（复用模型下载器的原语） ----

def _download_asset(url: str, size: int, sha: str, tmp: Path,
                    report) -> None:  # report(done, total, label)
    from ..models import manager as _m

    opener = _m._opener()  # noqa: SLF001 — 同仓库内部复用（代理设置一致）
    _m._download_to(url, tmp, opener, report, 0, size, tmp.name)  # noqa: SLF001
    if size and tmp.stat().st_size != size:
        raise RuntimeError(f"{tmp.name} 大小不符")
    if sha and _m._sha256(tmp) != sha:  # noqa: SLF001
        raise RuntimeError(f"{tmp.name} sha256 校验失败")


def _extract(archive: Path, dest: Path) -> int:
    """整包解压到 dest；返回解出的字节数（进度由 _progress_sampler 旁路采样）。"""
    import py7zr

    with py7zr.SevenZipFile(archive) as z:
        z.extractall(path=str(dest))
    return sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())


def _progress_sampler(stop: threading.Event, dest: Path, total: int) -> None:
    """extract 阶段的旁路进度：每 2s 扫描落盘字节数。"""
    while not stop.wait(2.0):
        try:
            got = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
        except OSError:
            continue
        with _state_lock:
            _state["done"] = min(got, total)


def _publish(bus: EventBus, **ev) -> None:
    bus.publish({"type": "trt_component", **ev})


def _install(bus: EventBus, arch: str | None) -> None:
    """线程体：core + 匹配架构的 builder 包 → staging → 原子换名。"""
    from ..server import engine_select

    assets = load_assets().get("assets", {})
    core = assets.get("core")
    if not core:
        raise RuntimeError("组件资产清单缺失（trt_component_assets.json）")
    key = arch if arch and f"builder-{arch}" in assets else "ptx"
    parts = [("core", core)] + (
        [(f"builder-{key}", assets[f"builder-{key}"])] if f"builder-{key}" in assets else [])
    total_dl = sum(p["size"] for _, p in parts)
    tmp_dir = ROOT / ".tmp" / "trt_component"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    def report(done, total, label):
        with _state_lock:
            _state.update(phase="download", file=label, done=done, total=total)
        _publish(bus, phase="download", file=label, done=done, total=total)

    try:
        if _STAGING.exists():
            shutil.rmtree(_STAGING)
        _STAGING.mkdir(parents=True)
        dl_done = 0
        for name, part in parts:
            arch_path = tmp_dir / f"{name}.7z"
            try:
                _download_asset(part["url"], part["size"], part["sha256"], arch_path,
                                lambda d, t, l, _base=dl_done: report(_base + d, total_dl, l))
            except Exception:  # noqa: BLE001 — 单包失败整体失败，清理半成品
                arch_path.unlink(missing_ok=True)
                raise
            dl_done += part["size"]
            with _state_lock:
                _state.update(phase="extract", file=name, done=0,
                              total=part.get("raw", 0))
            _publish(bus, phase="extract", file=name)
            stop = threading.Event()
            sampler = threading.Thread(
                target=_progress_sampler, args=(stop, _STAGING, part.get("raw", 0)),
                daemon=True)
            sampler.start()
            try:
                _extract(arch_path, _STAGING)
            finally:
                stop.set()
                sampler.join(timeout=3)
            arch_path.unlink(missing_ok=True)

        # 写 manifest（版本/ABI 信息由资产清单带出）
        assets_meta = load_assets()
        manifest = {
            "component": "trt", "version": assets_meta.get("version", 1),
            "python": assets_meta.get("python", ""),
            "ort": assets_meta.get("ort", ""),
            "trt": assets_meta.get("trt", ""),
        }
        (_STAGING / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        if _OLD.exists():
            shutil.rmtree(_OLD)
        if COMPONENT_DIR.exists():
            COMPONENT_DIR.rename(_OLD)
        _STAGING.rename(COMPONENT_DIR)
        shutil.rmtree(_OLD, ignore_errors=True)
        with _state_lock:
            _state.update(phase="done", installing=False, done=0, total=0, error=None)
        _publish(bus, phase="done")
        engine_select._PROBE_CACHE.clear()  # noqa: SLF001 — 装完重探测
    except Exception as e:  # noqa: BLE001 — 任何失败都要回到 UI
        shutil.rmtree(_STAGING, ignore_errors=True)
        with _state_lock:
            _state.update(phase="error", installing=False, error=str(e))
        _publish(bus, phase="error", error=str(e))


def start_install(bus: EventBus) -> tuple[bool, str]:
    """启动安装线程；已在装/有任务在跑时拒绝。返回 (accepted, 说明)。"""
    global _install_thread
    with _state_lock:
        if _state["installing"]:
            return False, "组件正在安装中"
        _state.update(installing=True, phase="download", file="", done=0,
                      total=0, error=None)
    from .app import runner  # 循环导入延迟到调用点

    if runner.current_id is not None:
        with _state_lock:
            _state.update(installing=False, phase=None)
        return False, "有任务正在运行，请等任务结束后再安装"

    arch = detect_gpu_arch()
    _install_thread = threading.Thread(
        target=_install_safe, args=(bus, arch), daemon=True, name="trt-install")
    _install_thread.start()
    return True, ""


def _install_safe(bus: EventBus, arch: str | None) -> None:
    try:
        _install(bus, arch)
    except Exception as e:  # noqa: BLE001 — 线程体兜底
        shutil.rmtree(_STAGING, ignore_errors=True)
        with _state_lock:
            _state.update(phase="error", installing=False, error=str(e))
        _publish(bus, phase="error", error=str(e))


def uninstall() -> tuple[bool, str]:
    """删除组件目录（占用中 DLL 会删失败，由调用方提示重启后重试）。"""
    with _state_lock:
        if _state["installing"]:
            return False, "组件正在安装中"
    from .app import runner

    if runner.current_id is not None:
        return False, "有任务正在运行，请等任务结束后再卸载"
    if not COMPONENT_DIR.exists():
        return True, ""
    shutil.rmtree(_OLD, ignore_errors=True)
    try:
        COMPONENT_DIR.rename(_OLD)
        shutil.rmtree(_OLD)
    except OSError as e:
        # worker 还攥着 DLL：把目录换名回去，保持 installed 状态
        if _OLD.exists() and not COMPONENT_DIR.exists():
            _OLD.rename(COMPONENT_DIR)
        return False, f"文件被占用，请退出应用后重试（{e}）"
    from ..server import engine_select

    engine_select._PROBE_CACHE.clear()  # noqa: SLF001
    return True, ""
