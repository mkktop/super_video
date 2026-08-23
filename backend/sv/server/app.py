"""FastAPI sidecar：HTTP + WS 对外接口，串行队列调度。"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..models import manager
from ..models.registry import ModelNotFoundError, load_registry
from ..pipeline.probe import UnsupportedMedia, probe, validate_m0
from . import db
from .events import EventBus
from .hardware import hardware_info
from .runner import Runner

bus = EventBus()
runner = Runner(bus)
_download_lock = asyncio.Lock()


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
    params: dict = Field(default_factory=dict)  # scale/codec/crf/preset/tile 每任务独立


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": app.version}


@app.get("/api/hardware")
def get_hardware() -> dict:
    return hardware_info()


@app.get("/api/models")
def get_models() -> list[dict]:
    out = []
    for spec in load_registry().values():
        out.append({
            "id": spec.id, "name": spec.name, "scale": spec.scale,
            "content": spec.content, "speed": spec.speed,
            "vram_gb": spec.vram_gb, "description": spec.description,
            "tile_hint": spec.tile_hint,
            "installed": manager.is_downloaded(spec),
        })
    return out


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
    if params.get("codec") not in (None, "h264", "h265"):
        raise HTTPException(400, "codec 仅支持 h264/h265")
    crf = params.get("crf", 18)
    if not (0 <= int(crf) <= 51):
        raise HTTPException(400, "crf 范围 0-51")
    params["crf"] = int(crf)

    out = body.output or str(
        input_path.with_name(f"{input_path.stem}_{scale}x{input_path.suffix or '.mp4'}")
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


@app.get("/api/tasks/{task_id}/preview")
def task_preview(task_id: str):
    t = db.get_task(task_id)
    if t is None:
        raise HTTPException(404)
    p = t.get("preview_path")
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
