"""视频剪切路由：轻量后台任务（与超分队列独立；产物可直接作为超分输入）。"""
from __future__ import annotations

import queue
import threading
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...pipeline.probe import UnsupportedMedia, probe
from ...pipeline.trim import run_trim
from ..settings import load as load_settings
from ..state import bus, cached_hardware
from ..gpu_lease import acquire as _gpu_acquire, release as _gpu_release

router = APIRouter(tags=["trim"])


class TrimCreate(BaseModel):
    input: str
    start_s: float = Field(ge=0)
    end_s: float
    mode: str = "smart"  # smart | fast | exact
    output: str | None = None
    overwrite: bool = False  # 显式 output 撞已存在文件时 409，确认覆盖后带 True 重交


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
    from ...utils.process import kill_tree

    from ...pipeline.trim import TrimCanceled

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
            _gpu_acquire("trim")  # 与队列任务/对比作业显存互斥（见 gpu_lease 模块注释）
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
            finally:
                _gpu_release("trim")
        except (TrimCanceled, Exception) as e:  # noqa: BLE001 — 后台线程兜底
            if job.get("cancel_requested") or isinstance(e, TrimCanceled):
                job.update(state="canceled", error=None)
                bus.publish_threadsafe({"type": "trim", "job_id": jid, "state": "canceled"})
            else:
                job.update(state="failed", error=str(e))


def _ts_label(t: float) -> str:
    m, s = divmod(int(t), 60)
    return f"{m:02d}-{s:02d}"


@router.post("/api/trim", status_code=201)
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
    output = body.output
    if not output:
        # 与超分任务同一规则：全局输出目录优先，否则源视频同目录
        _odir = str(load_settings().get("output_dir") or "").strip()
        _root = Path(_odir) if _odir else input_path.parent
        output = str(_root / f"{input_path.stem}_cut_{_ts_label(body.start_s)}-{_ts_label(body.end_s)}.mp4")
        try:
            _root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(400, f"无法创建输出目录 {_root}: {e}") from e
    elif Path(output).exists() and not body.overwrite:
        # 自动命名带时间戳不会撞；显式路径撞已存在文件时 409 让前端确认
        raise HTTPException(409, f"输出文件已存在：{output}")
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


@router.get("/api/trim/{job_id}")
def trim_status(job_id: str) -> dict:
    job = _trim_jobs.get(job_id)
    if job is None:
        raise HTTPException(404)
    # proc（Popen 句柄）等内部字段不能进 JSON
    return {k: v for k, v in job.items() if k != "proc"}


@router.post("/api/trim/{job_id}/cancel")
def cancel_trim(job_id: str) -> dict:
    """取消排队/进行中的剪切：杀当前 ffmpeg 段，后续段不再执行。"""
    from ...utils.process import kill_tree

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
