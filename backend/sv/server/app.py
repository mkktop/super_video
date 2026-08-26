"""FastAPI sidecar：HTTP + WS 对外接口，串行队列调度。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import shutil
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..models import manager
from ..models.registry import (
    BUNDLED_DIR,
    USER_REGISTRY_DIR,
    ModelNotFoundError,
    get_model,
    load_registry,
    model_dir,
)
from ..paths import ROOT, TEMP_DIR, migrate_legacy_data
from ..pipeline.probe import UnsupportedMedia, probe, validate_m0
from ..pipeline.trim import run_trim
from . import db
from . import trt_component
from .engine_select import select_engine
from .events import EventBus
from .hardware import hardware_info
from .perf import INTERVAL_S, PerfSampler
from .runner import Runner
from .settings import DEFAULTS, load as load_settings, save as save_settings

log = logging.getLogger("sv.app")

bus = EventBus()
runner = Runner(bus)
perf = PerfSampler(bus, runner)
_download_lock = asyncio.Lock()


def _expected_tokens() -> set[str]:
    """本地令牌（未配置=开发/测试模式不鉴权）。

    监听 127.0.0.1 并非浏览器隔离：任意网页可直接 POST 本端口（CORS 挡不住
    no-cors/表单 POST，WS 更不受 CORS 约束）。带上随机 token 后恶意页面无从
    获得令牌，本机 API 不再是 drive-by 攻击面。

    两个来源取并集：
    - SV_TOKEN 环境变量：Electron 拉起 sidecar 时注入（本次会话令牌）；
    - TEMP_DIR/sidecar.token 文件：UI 重启生成新 token 复用旧 sidecar 时，
      旧进程 env 里还是老 token——按文件实时校验才能接受新 token。
    """
    toks: set[str] = set()
    env_tok = os.environ.get("SV_TOKEN")
    if env_tok:
        toks.add(env_tok)
    try:
        f = TEMP_DIR / "sidecar.token"
        if f.is_file():
            t = f.read_text(encoding="utf-8").strip()
            if t:
                toks.add(t)
    except OSError:
        pass
    return toks


async def _token_auth(request: Request, call_next):
    toks = _expected_tokens()
    if toks and request.method != "OPTIONS" and request.url.path != "/api/health":
        got = request.headers.get("x-sv-token") or request.query_params.get("token")
        if got not in toks:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)

# 硬件探测含 nvidia-smi/NVENC 试编码，按 60s 缓存
_hw_cache: tuple[float, dict] = (0.0, {})


def cached_hardware() -> dict:
    global _hw_cache
    ts, data = _hw_cache
    if not data or time.time() - ts > 60:
        data = hardware_info()
        _hw_cache = (time.time(), data)
    return data


_PRESETS_PATH = Path(__file__).parent / "presets.json"


def _presets() -> list[dict]:
    return json.loads(_PRESETS_PATH.read_text(encoding="utf-8"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 一次性迁移 ≤v0.1.20 遗留在安装目录里的数据（模型/组件/设置/缓存）。
    # 必须在 init_db 之前——任务库也在 .tmp 里
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, migrate_legacy_data)
    db.init_db()
    await loop.run_in_executor(None, sweep_orphan_workdirs)
    _start_trim_worker()
    runner.start()
    perf.start()
    yield
    await runner.stop()
    perf.stop()


app = FastAPI(title="super_video sidecar", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
app.middleware("http")(_token_auth)


# ---- 模型 ----


class TaskCreate(BaseModel):
    input: str
    output: str | None = None
    model_id: str
    params: dict = Field(default_factory=dict)  # scale/codec/crf/preset/tile/interp/denoise 每任务独立


class ModelImport(BaseModel):
    path: str
    id: str = Field(min_length=2, max_length=48)
    name: str = Field(min_length=1, max_length=48)
    scale: int = Field(ge=2, le=8)
    color: str = "rgb"            # rgb | bgr
    value_range: str = "0-1"      # 0-1 | 0-255
    tile: int = 0
    description: str = ""


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": app.version}


@app.get("/api/hardware")
def get_hardware() -> dict:
    return cached_hardware()


@app.get("/api/perf/history")
def perf_history() -> dict:
    """性能采样快照:本次启动内的全部历史(环形缓冲最近 1 小时)。"""
    return {"interval_s": INTERVAL_S, "samples": perf.snapshot()}


@app.get("/api/engine")
async def get_engine() -> dict:
    """推理后端展示：任务进行中如实返回当前任务所用后端；
    空闲时按设置推算下一任务将使用的后端（与 runner._run_one 同口径，
    设置切换后立即反映）。探测首次可达分钟级，放线程池避免阻塞事件循环。"""
    if runner.current_id is not None and runner.engine is not None:
        e = runner.engine
    else:
        engine_setting = load_settings().get("engine", "auto")
        want = None if engine_setting == "auto" else engine_setting
        loop = asyncio.get_running_loop()
        e = await loop.run_in_executor(None, select_engine, want)
    return {"backend": e.backend, "python": e.python_exe, "detail": e.detail}


@app.get("/api/trt-component")
def get_trt_component() -> dict:
    """TRT 可选组件状态（安装/版本/体积/显卡架构/资产清单/安装进度）。"""
    return trt_component.status()


@app.post("/api/trt-component/install")
def install_trt_component() -> dict:
    accepted, msg = trt_component.start_install(bus)
    if not accepted:
        raise HTTPException(409, msg)
    return {"ok": True}


@app.delete("/api/trt-component")
def uninstall_trt_component() -> dict:
    ok, msg = trt_component.uninstall()
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True}


@app.get("/api/presets")
def get_presets() -> list[dict]:
    return _presets()


class ProbeBody(BaseModel):
    path: str


@app.post("/api/probe")
def probe_media(body: ProbeBody) -> dict:
    """向导第一步：媒体信息 + M0 可行性（供 UI 展示与预估）。"""
    p = Path(body.path)
    if not p.exists():
        raise HTTPException(400, f"文件不存在: {body.path}")
    try:
        info = probe(p)
        m0_error = None
        try:
            validate_m0(info)
        except UnsupportedMedia as e:
            m0_error = str(e)
    except UnsupportedMedia as e:
        raise HTTPException(422, str(e))
    return {
        "ok": m0_error is None,
        "error": m0_error,
        "width": info.width, "height": info.height,
        "fps": round(info.fps, 3),
        "duration_s": round(info.duration_s, 2),
        "total_frames": info.total_frames,
        "codec": info.video_codec, "pix_fmt": info.pix_fmt,
        "has_audio": info.has_audio,
        "subtitles": info.subtitles,
    }


@app.get("/api/settings")
def get_settings() -> dict:
    return load_settings()


@app.get("/api/log-tail")
def log_tail(n: int = 120) -> dict:
    p = TEMP_DIR / "sidecar.log"
    if not p.exists():
        return {"lines": []}
    # 从文件尾按块回读，不整读（日志大时全量 read_text 是内存尖峰）
    try:
        with p.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                step = min(4096, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
        return {"lines": data.decode("utf-8", "replace").splitlines()[-n:]}
    except OSError:
        return {"lines": []}


@app.put("/api/settings")
def put_settings(body: dict) -> dict:
    try:
        return save_settings(body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/models")
def get_models() -> list[dict]:
    hw = cached_hardware()
    gpu_vram = next(
        (g["vram_gb"] for g in hw.get("gpus", []) if g.get("vram_gb")), None
    )
    out = []
    for spec in load_registry().values():
        bundled = bool(spec.files) and all(
            (BUNDLED_DIR / f["name"]).exists() for f in spec.files
        )
        size_mb = round(sum(f.get("size", 0) for f in spec.files) / 1e6, 1)
        vram_ok = gpu_vram is None or spec.vram_gb <= gpu_vram
        # denoise 档位：registry files 里 variant=denoiseN 的集合（real-cugan 专属概念）
        denoise_levels = sorted({
            int(f["variant"][7:]) for f in spec.files
            if str(f.get("variant", "")).startswith("denoise")
            and str(f["variant"])[7:].isdigit()
        })
        out.append({
            "id": spec.id, "name": spec.name, "scale": spec.scale,
            "kind": spec.kind,
            "content": spec.content, "speed": spec.speed,
            "vram_gb": spec.vram_gb, "description": spec.description,
            "tile_hint": spec.tile_hint,
            "installed": manager.is_downloaded(spec),
            "bundled": bundled,
            "size_mb": size_mb,
            "vram_ok": vram_ok,
            "vram_note": None if vram_ok else (
                f"需要约 {spec.vram_gb}GB 显存，本机 {gpu_vram}GB"
            ),
            "denoise_levels": denoise_levels,
        })
    return out


@app.delete("/api/models/{model_id}")
def delete_model(model_id: str) -> dict:
    specs = load_registry()
    if model_id not in specs:
        raise HTTPException(404, f"未知模型 {model_id}")
    d = model_dir(model_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    # 自定义导入的模型：连 manifest 一起删（内置模型保留注册表条目）
    custom = USER_REGISTRY_DIR / f"{model_id}.json"
    if custom.exists():
        custom.unlink()
    return {"ok": True}


@app.post("/api/models/import", status_code=201)
def import_model(body: ModelImport) -> dict:
    """自定义 ONNX 导入：拷贝入库 -> 真跑一帧验证 -> 写 manifest。"""
    import re as _re
    import shutil as _shutil

    import numpy as np

    from ..engines.onnx_engine import OnnxSrEngine

    src = Path(body.path)
    if not src.exists() or src.suffix.lower() != ".onnx":
        raise HTTPException(400, f"ONNX 文件不存在: {body.path}")
    if not _re.fullmatch(r"[a-z0-9][a-z0-9-]+", body.id):
        raise HTTPException(400, "id 仅允许小写字母/数字/连字符")
    if body.color not in ("rgb", "bgr") or body.value_range not in ("0-1", "0-255"):
        raise HTTPException(400, "color 仅 rgb/bgr；value_range 仅 0-1/0-255")
    if body.id in load_registry():
        raise HTTPException(409, f"模型 id 已存在: {body.id}")

    d = model_dir(body.id)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / src.name
    _shutil.copy2(src, dest)

    io = {"color": body.color, "range": body.value_range}
    try:
        eng = OnnxSrEngine(dest, body.scale, io=io, tile=body.tile)
        eng.load()
        out = eng.process(np.random.default_rng(0).integers(0, 256, (64, 64, 3), dtype=np.uint8))
        h, w = out.shape[:2]
        if (w, h) != (64 * body.scale, 64 * body.scale):
            raise ValueError(f"输出 {w}x{h} 与 x{body.scale} 预期不符，请核对倍率")
        if float(out.std()) < 1.0:
            raise ValueError("输出接近纯色，IO 约定（颜色序/值域）可能不对")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — 校验失败回滚拷贝
        _shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(422, f"模型验证失败: {e}") from e

    manifest = {
        "id": body.id, "name": body.name, "vendor": "自定义", "license": "用户自带",
        "engine": "onnx", "kind": "sr", "scale": [body.scale],
        "content": [], "temporal": False, "speed": "balanced", "vram_gb": 4,
        "tile_hint": int(body.tile), "description": body.description or "用户导入的 ONNX 模型",
        "io": io, "files": [{"name": src.name}],
    }
    USER_REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    (USER_REGISTRY_DIR / f"{body.id}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"ok": True, "id": body.id}


@app.post("/api/models/{model_id}/download")
async def download_model(model_id: str) -> dict:
    specs = load_registry()
    if model_id not in specs:
        raise HTTPException(404, f"未知模型 {model_id}")
    spec = specs[model_id]
    if manager.is_downloaded(spec):
        return {"ok": True, "already": True}
    if _download_lock.locked():
        raise HTTPException(409, "已有下载在进行中")

    async def _do():
        async with _download_lock:
            # 排队窗口内前一个下载可能已完成：复查后直接补发完成事件，不重跑
            if manager.is_downloaded(spec):
                bus.publish({"type": "model_download", "model_id": model_id, "done": True})
                return
            loop = asyncio.get_running_loop()
            total = sum(f.get("size", 0) for f in spec.files) or 1

            def cb(done, tot, label):
                # executor 线程回调：asyncio.Queue 非线程安全，必须回事件循环线程投递
                bus.publish_threadsafe({
                    "type": "model_download", "model_id": model_id,
                    "progress": round(done / total, 4),
                })

            try:
                await loop.run_in_executor(None, manager.download, spec, cb)
                # 下载完成后生成 fp16 变体（bundled 模型随包已有，此处只补下载件）
                try:
                    from ..models.fp16 import ensure_fp16

                    await loop.run_in_executor(None, ensure_fp16, spec)
                except ImportError:
                    log.warning("fp16 转换依赖缺失（onnx/onnxconverter-common），保持 fp32")
                except Exception as e:  # noqa: BLE001 — 转换失败不影响使用 fp32
                    log.warning(f"fp16 转换失败，保持 fp32: {e}")
                bus.publish({"type": "model_download", "model_id": model_id, "done": True})
            except Exception as e:  # noqa: BLE001
                bus.publish({"type": "model_download", "model_id": model_id,
                             "failed": str(e)})

    asyncio.create_task(_do())
    return {"ok": True, "started": True}


# ---- 任务 ----


# codec → 硬件能力位（None = 软编总是可用）
_CODECS = {
    "h264": None, "h265": None,
    "h264_nvenc": "nvenc", "hevc_nvenc": "nvenc", "av1_nvenc": "av1_nvenc",
    "h264_amf": "amf", "hevc_amf": "amf",
    "av1_svt": "svt_av1",
}
_AUDIO_MODES = ("auto", "copy", "aac", "flac", "none")
_CONTAINERS = ("mp4", "mkv", "mov")


_PRESETS_XCODE = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)


@app.post("/api/tasks", status_code=201)
def create_task(body: TaskCreate) -> dict:
    input_path = Path(body.input)
    if not input_path.exists():
        raise HTTPException(400, f"输入文件不存在: {body.input}")
    try:
        specs = load_registry()
        if body.model_id not in specs:
            raise HTTPException(404, f"未知模型 {body.model_id}")
        spec = specs[body.model_id]
        info = probe(input_path)
        validate_m0(info)
    except UnsupportedMedia as e:
        raise HTTPException(422, str(e))

    params = dict(body.params)
    scale = int(params.get("scale") or max(spec.scale))
    if scale not in spec.scale:
        raise HTTPException(400, f"模型 {spec.id} 不支持 x{scale}，可选 {spec.scale}")
    params["scale"] = scale
    target = int(params.get("target_scale") or scale)
    if not (1 <= target <= scale):
        raise HTTPException(400, f"目标倍率需在 x1 ~ x{scale} 之间")
    params["target_scale"] = target
    # 自定义分辨率：精确目标宽高，优先于整数倍率；纪律=原生超分后只缩不放
    tw, th = params.get("target_w"), params.get("target_h")
    if tw is not None or th is not None:
        if not (isinstance(tw, int) and isinstance(th, int) and tw > 0 and th > 0):
            raise HTTPException(400, "自定义分辨率需同时提供正整数 target_w/target_h")
        tw, th = tw - tw % 2, th - th % 2  # yuv420 编码要求偶数，向下取整
        if tw < info.width or th < info.height:
            raise HTTPException(
                400, f"目标分辨率不能低于源 {info.width}x{info.height}（当前 {tw}x{th}）")
        if tw > info.width * scale or th > info.height * scale:
            raise HTTPException(400, (
                f"目标 {tw}x{th} 超出模型 x{scale} 原生上限 "
                f"{info.width * scale}x{info.height * scale}，请提高放大倍数或换更高倍率模型"
            ))
        params["target_w"], params["target_h"] = tw, th
        params["target_scale"] = scale  # 整数倍率字段退位为模型原生档
    tile = int(params.get("tile", 0))
    if tile != 0 and not (64 <= tile <= 4096 and tile % 2 == 0):
        raise HTTPException(400, "tile 需为 0（自动）或 64~4096 的偶数像素")
    params["tile"] = tile
    out_kind = params.get("out_kind", "video")
    if out_kind not in ("video", "png", "jpg"):
        raise HTTPException(400, "out_kind 仅支持 video / png / jpg")
    params["out_kind"] = out_kind
    codec = params.get("codec", "h264")
    if codec not in _CODECS:
        raise HTTPException(400, f"codec 仅支持 {' / '.join(_CODECS)}")
    hw_flag = _CODECS[codec]
    if hw_flag and not cached_hardware().get(hw_flag):
        raise HTTPException(400, f"本机 {hw_flag} 不可用，请选择软件编码")
    params["codec"] = codec
    container = params.get("container", "mp4")
    if container not in _CONTAINERS:
        raise HTTPException(400, "container 仅支持 mp4 / mkv / mov")
    params["container"] = container
    audio_mode = params.get("audio_mode", "auto")
    if audio_mode not in _AUDIO_MODES:
        raise HTTPException(400, f"audio_mode 仅支持 {' / '.join(_AUDIO_MODES)}")
    if audio_mode == "flac" and container != "mkv":
        raise HTTPException(400, "FLAC 音轨仅支持 mkv 容器（mp4 请选 aac）")
    params["audio_mode"] = audio_mode
    subtitle_mode = params.get("subtitle_mode", "none")
    if subtitle_mode not in ("none", "auto"):
        raise HTTPException(400, "subtitle_mode 仅支持 none / auto")
    params["subtitle_mode"] = subtitle_mode
    interp = params.get("interp", "off")
    if interp not in ("off", "rife2x"):
        raise HTTPException(400, "interp 仅支持 off / rife2x")
    params["interp"] = interp
    if params.get("denoise") is not None:
        if int(params["denoise"]) not in (0, 1, 2, 3):
            raise HTTPException(400, "denoise 仅支持 0 / 1 / 2 / 3")
        params["denoise"] = int(params["denoise"])
    crf = params.get("crf", 18)
    if not (0 <= int(crf) <= 51):
        raise HTTPException(400, "crf 范围 0-51")
    params["crf"] = int(crf)
    preset = params.get("preset", "medium")
    if preset not in _PRESETS_XCODE:
        raise HTTPException(400, f"preset 仅支持 {' / '.join(_PRESETS_XCODE)}")
    params["preset"] = preset
    for k, hi in (("batch", 16), ("chunk", 1024)):
        v = params.get(k)
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= hi):
                raise HTTPException(400, f"{k} 需为 1~{hi} 的整数")
            params[k] = v

    res_label = f"{tw}x{th}" if (tw is not None and th is not None) else f"{target}x"
    if out_kind == "video":
        out = body.output or str(
            input_path.with_name(f"{input_path.stem}_{res_label}.{container}")
        )
    else:
        # 图片序列：输出是文件夹，帧图按 000001.png 起逐帧编号
        out = body.output or str(input_path.with_name(f"{input_path.stem}_{res_label}_frames"))
        if Path(out).exists() and not Path(out).is_dir():
            raise HTTPException(400, "图片序列的输出路径需为文件夹")
    task = db.new_task(
        str(input_path), out, body.model_id, params,
        src={"w": info.width, "h": info.height, "fps": info.fps,
             "total_frames": info.total_frames},
    )
    bus.publish({"type": "task_status", "task_id": task["id"], "status": "queued"})
    return task


def _with_queue_pos(tasks: list[dict]) -> list[dict]:
    qpos = 0
    for t in tasks:
        if t["status"] == "queued":
            qpos += 1
            t["queue_position"] = qpos
        else:
            t["queue_position"] = None
    return tasks


@app.get("/api/tasks")
def get_tasks() -> list[dict]:
    return _with_queue_pos(db.list_tasks())


@app.get("/api/stats")
def get_stats() -> dict:
    return db.stats()


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    t = db.get_task(task_id)
    if t is None:
        raise HTTPException(404)
    return t


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict:
    ok = await runner.cancel(task_id)
    if not ok:
        raise HTTPException(409, "任务不存在或不可取消")
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
async def remove_task(task_id: str) -> dict:
    t = db.get_task(task_id)
    if t is None:
        raise HTTPException(404)
    if t["status"] == "running":
        raise HTTPException(409, "任务运行中，请先取消")
    if not db.delete_task(task_id):
        raise HTTPException(409, "删除失败")
    # 工作目录（续跑用）与预览图随任务删除一并清理；大目录可能数百 MB，
    # 放后台线程执行不阻塞响应
    asyncio.get_running_loop().run_in_executor(None, purge_task_files, task_id)
    # 广播删除事件：WS 健康时轮询间隔 8s，靠事件让列表即时刷新
    bus.publish({"type": "task_deleted", "task_id": task_id})
    return {"ok": True}


def purge_task_files(task_id: str) -> None:
    """删除任务专属临时产物：分段/分块工作目录 + 预览图。"""
    for d in (TEMP_DIR / "segmented" / task_id, TEMP_DIR / "chunked" / task_id):
        shutil.rmtree(d, ignore_errors=True)
    pv = TEMP_DIR / "previews"
    if pv.is_dir():
        for p in pv.glob(f"{task_id}*.jpg"):
            try:
                p.unlink()
            except OSError:
                pass


def sweep_orphan_workdirs(max_age_s: float = 3600.0) -> None:
    """启动清扫：崩溃/强杀遗留的孤儿工作目录（数据库无对应任务）。

    只清修改时间超过 max_age_s 的目录——避免误删其他 sidecar 实例
    （测试/开发）刚建的任务目录。取消任务的目录因任务行还在而保留（续跑依据）。
    """
    ids = db.all_task_ids()
    deadline = time.time() - max_age_s
    for sub in ("segmented", "chunked"):
        base = TEMP_DIR / sub
        if not base.is_dir():
            continue
        for d in base.iterdir():
            try:
                if d.is_dir() and d.name not in ids and d.stat().st_mtime < deadline:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue


class TaskReorder(BaseModel):
    ids: list[str]


@app.post("/api/tasks/reorder")
def reorder_tasks(body: TaskReorder) -> dict:
    """按传入顺序重排排队任务（拖拽排序）；非排队 id 自动跳过。"""
    n = db.reorder_queued(body.ids)
    return {"ok": True, "reordered": n}


# ---- 视频剪切（轻量后台任务，与超分队列独立；产物可直接作为超分输入）----


class TrimCreate(BaseModel):
    input: str
    start_s: float = Field(ge=0)
    end_s: float
    mode: str = "smart"  # smart | fast | exact
    output: str | None = None


_trim_jobs: dict[str, dict] = {}
_trim_queue: "queue.Queue[str]" = queue.Queue()
_trim_thread: threading.Thread | None = None


def _start_trim_worker() -> None:
    """lifespan 里启动（import 即起线程对测试不友好；幂等防多次启停重启）"""
    global _trim_thread
    if _trim_thread is not None and _trim_thread.is_alive():
        return
    _trim_thread = threading.Thread(target=_trim_worker, daemon=True, name="trim-worker")
    _trim_thread.start()


def _trim_worker() -> None:
    from ..pipeline.trim import TrimCanceled
    from ..utils.process import kill_tree

    while True:
        jid = _trim_queue.get()
        job = _trim_jobs.get(jid)
        if job is None:
            continue
        if job.get("cancel_requested"):  # 排队期被取消：不再执行
            job.update(state="canceled", error=None)
            continue
        job["proc"] = None

        def _spawn_hook(proc, _job=job):
            _job["proc"] = proc  # 取消端点据此 kill

        try:
            res = run_trim(
                job["input"], job["start_s"], job["end_s"], job["mode"], job["output"],
                nvenc=bool(cached_hardware().get("nvenc")),
                progress_cb=lambda p: job.update(state="running", progress=round(p, 4)),
                on_spawn=_spawn_hook,
                cancel_check=lambda: bool(job.get("cancel_requested")),
            )
            job.update(
                state="done", progress=1.0, output=str(res.output),
                actual_start_s=round(res.actual_start, 3),
                duration_s=round(res.duration_s, 3),
                mode=res.mode, notices=res.notices,
            )
        except (TrimCanceled, Exception) as e:  # noqa: BLE001 — 后台线程兜底
            if job.get("cancel_requested") or isinstance(e, TrimCanceled):
                job.update(state="canceled", error=None)
                bus.publish_threadsafe({"type": "trim", "job_id": jid, "state": "canceled"})
            else:
                job.update(state="failed", error=str(e))


def _ts_label(t: float) -> str:
    m, s = divmod(int(t), 60)
    return f"{m:02d}-{s:02d}"


@app.post("/api/trim", status_code=201)
def create_trim(body: TrimCreate) -> dict:
    input_path = Path(body.input)
    if not input_path.exists():
        raise HTTPException(400, f"输入文件不存在: {body.input}")
    if body.mode not in ("smart", "fast", "exact"):
        raise HTTPException(400, "mode 仅支持 smart / fast / exact")
    try:
        info = probe(input_path)
    except UnsupportedMedia as e:
        raise HTTPException(422, str(e))
    if body.end_s <= body.start_s:
        raise HTTPException(400, "出点必须晚于入点")
    if body.start_s >= info.duration_s:
        raise HTTPException(400, "入点超出视频时长")
    output = body.output or str(input_path.with_name(
        f"{input_path.stem}_cut_{_ts_label(body.start_s)}-{_ts_label(body.end_s)}.mp4"
    ))
    # 简单 GC：完成态任务超过 50 个时丢最旧的
    done_ids = [k for k, v in _trim_jobs.items() if v["state"] in ("done", "failed")]
    for k in done_ids[: max(0, len(done_ids) - 50)]:
        _trim_jobs.pop(k, None)
    jid = uuid4().hex[:12]
    _trim_jobs[jid] = {
        "state": "queued", "progress": 0.0, "input": str(input_path),
        "start_s": body.start_s, "end_s": body.end_s, "mode": body.mode,
        "output": output, "error": None, "cancel_requested": False,
    }
    _start_trim_worker()  # 懒启动（幂等）：TestClient 不触发 lifespan 也能跑
    _trim_queue.put(jid)
    return {"job_id": jid, "output": output}


@app.get("/api/trim/{job_id}")
def trim_status(job_id: str) -> dict:
    job = _trim_jobs.get(job_id)
    if job is None:
        raise HTTPException(404)
    # proc（Popen 句柄）等内部字段不能进 JSON
    return {k: v for k, v in job.items() if k != "proc"}


@app.post("/api/trim/{job_id}/cancel")
def cancel_trim(job_id: str) -> dict:
    """取消排队/进行中的剪切：杀当前 ffmpeg 段，后续段不再执行。"""
    from ..utils.process import kill_tree

    job = _trim_jobs.get(job_id)
    if job is None:
        raise HTTPException(404)
    if job["state"] not in ("queued", "running"):
        raise HTTPException(409, "任务已结束")
    job["cancel_requested"] = True
    proc = job.get("proc")
    if proc is not None and proc.poll() is None:
        kill_tree(proc.pid)
    if job["state"] == "queued":  # 还没被 worker 领走：直接落终态
        job.update(state="canceled", error=None)
    return {"ok": True}


@app.post("/api/tasks/{task_id}/resume")
def resume_task(task_id: str) -> dict:
    """续跑失败/取消的任务：回到队列，worker 按 checkpoint 跳过已完成部分。"""
    t = db.get_task(task_id)
    if t is None:
        raise HTTPException(404)
    if t["status"] not in ("failed", "canceled"):
        raise HTTPException(409, "仅失败或取消的任务可续跑")
    if not Path(t["input_path"]).exists():
        raise HTTPException(409, "输入文件已不存在，无法续跑")
    try:
        spec = get_model(t["model_id"])
    except ModelNotFoundError:
        raise HTTPException(409, "模型已不存在，无法续跑")
    # 流式(ONNX)任务续跑依赖分段工作目录；无目录时仅当任务从未开始（无进度）才允许从头重跑。
    # torch 任务无目录则从头跑（解码是幂等的）
    if spec.engine == "onnx" and not (TEMP_DIR / "segmented" / task_id).exists() \
            and (t["progress_frames"] or 0) > 0:
        raise HTTPException(409, "续跑数据已不存在（临时目录被清理），请新建任务")
    # 半成品输出已在失败/取消时删除；万一残留，续跑前清掉避免混淆
    try:
        Path(t["output_path"]).unlink(missing_ok=True)
    except OSError:
        pass
    db.update_task(task_id, status="queued", error=None, progress_frames=0)
    bus.publish({"type": "task_status", "task_id": task_id, "status": "queued"})
    return {"ok": True}


@app.get("/api/tasks/{task_id}/preview")
def task_preview(task_id: str, src: int = 0):
    """处理预览帧；?src=1 返回源首帧（对比预览左半边）。"""
    t = db.get_task(task_id)
    if t is None:
        raise HTTPException(404)
    p = t.get("preview_src") if src else t.get("preview_path")
    if not p or not Path(p).exists():
        raise HTTPException(404, "暂无预览")
    return FileResponse(p, media_type="image/jpeg")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    toks = _expected_tokens()
    if toks and ws.query_params.get("token") not in toks:  # WS 不受 CORS 约束，必须自查令牌
        await ws.close(code=4401)
        return
    await ws.accept()
    q = bus.subscribe()
    # 客户端不发消息；并发挂一个 receive 作断连哨兵，否则 q.get() 永远发现不了断开
    sentinel = asyncio.create_task(ws.receive())
    try:
        while True:
            send_task = asyncio.create_task(q.get())
            done, _ = await asyncio.wait(
                {sentinel, send_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if sentinel in done:
                send_task.cancel()
                break
            if send_task not in done:
                continue
            event = send_task.result()
            await ws.send_text(json.dumps(event, ensure_ascii=False))
    except Exception:  # 断开时 send 抛异常，走统一清理
        pass
    finally:
        sentinel.cancel()
        bus.unsubscribe(q)
