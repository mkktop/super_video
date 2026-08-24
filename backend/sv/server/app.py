"""FastAPI sidecar：HTTP + WS 对外接口，串行队列调度。"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..models import manager
from ..models.registry import (
    BUNDLED_DIR,
    USER_REGISTRY_DIR,
    ModelNotFoundError,
    get_model,
    load_registry,
    model_dir,
)
from ..paths import ROOT, TEMP_DIR
from ..pipeline.probe import UnsupportedMedia, probe, validate_m0
from . import db
from .engine_select import select_engine
from .events import EventBus
from .hardware import hardware_info
from .runner import Runner
from .settings import DEFAULTS, load as load_settings, save as save_settings

log = logging.getLogger("sv.app")

bus = EventBus()
runner = Runner(bus)
_download_lock = asyncio.Lock()

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
    db.init_db()
    runner.start()
    yield
    await runner.stop()


app = FastAPI(title="super_video sidecar", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


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


@app.get("/api/engine")
def get_engine() -> dict:
    """当前推理后端（runner 启动时探测：CUDA 优先，回落 DirectML）。"""
    if runner.engine is None:
        runner.engine = select_engine()
    e = runner.engine
    return {"backend": e.backend, "python": e.python_exe, "detail": e.detail}


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
    }


@app.get("/api/settings")
def get_settings() -> dict:
    return load_settings()


@app.get("/api/log-tail")
def log_tail(n: int = 120) -> dict:
    p = ROOT / ".tmp" / "sidecar.log"
    if not p.exists():
        return {"lines": []}
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    return {"lines": lines}


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
            loop = asyncio.get_running_loop()
            total = sum(f.get("size", 0) for f in spec.files) or 1

            def cb(done, tot, label):
                bus.publish({
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
    codec = params.get("codec", "h264")
    if codec not in ("h264", "h265", "h264_nvenc", "hevc_nvenc"):
        raise HTTPException(400, "codec 仅支持 h264/h265/h264_nvenc/hevc_nvenc")
    if codec.endswith("_nvenc") and not hardware_info().get("nvenc"):
        raise HTTPException(400, "本机 NVENC 不可用，请选择软件编码")
    params["codec"] = codec
    interp = params.get("interp", "off")
    if interp not in ("off", "rife2x"):
        raise HTTPException(400, "interp 仅支持 off / rife2x")
    params["interp"] = interp
    if params.get("denoise") is not None:
        if int(params["denoise"]) not in (0, 3):
            raise HTTPException(400, "denoise 仅支持 0 / 3")
        params["denoise"] = int(params["denoise"])
    crf = params.get("crf", 18)
    if not (0 <= int(crf) <= 51):
        raise HTTPException(400, "crf 范围 0-51")
    params["crf"] = int(crf)

    out = body.output or str(
        input_path.with_name(f"{input_path.stem}_{target}x{input_path.suffix or '.mp4'}")
    )
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
def remove_task(task_id: str) -> dict:
    t = db.get_task(task_id)
    if t is None:
        raise HTTPException(404)
    if t["status"] == "running":
        raise HTTPException(409, "任务运行中，请先取消")
    if not db.delete_task(task_id):
        raise HTTPException(409, "删除失败")
    return {"ok": True}


class TaskReorder(BaseModel):
    ids: list[str]


@app.post("/api/tasks/reorder")
def reorder_tasks(body: TaskReorder) -> dict:
    """按传入顺序重排排队任务（拖拽排序）；非排队 id 自动跳过。"""
    n = db.reorder_queued(body.ids)
    return {"ok": True, "reordered": n}


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
