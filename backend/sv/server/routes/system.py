"""系统域路由：健康/硬件/引擎/设置/日志尾/性能历史/队列完成动作取消。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from ... import __version__
from ...paths import TEMP_DIR
from ...pipeline.probe import DECODERS  # noqa: F401 — re-export 供既有测试 import
from ..engine_select import select_engine
from ..perf import INTERVAL_S
from ..settings import load as load_settings, save as save_settings
from ..state import cached_hardware, perf, runner

router = APIRouter(tags=["system"])


@router.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


@router.get("/api/hardware")
def get_hardware() -> dict:
    return cached_hardware()


@router.get("/api/perf/history")
def perf_history() -> dict:
    """性能采样快照:本次启动内的全部历史(环形缓冲最近 1 小时)。"""
    return {"interval_s": INTERVAL_S, "samples": perf.snapshot()}


@router.get("/api/engine")
async def get_engine() -> dict:
    """推理后端展示：backend/detail 恒为按当前设置推算的「下一任务将使用」后端
    （与 runner._run_one 同口径，保存后立即反映——任务运行中也不例外：用户切后端
    的高频时机正是任务在跑时，旧口径按任务实况返回会让设置页标签长时间不动，
    任务结束又无人回拉，只能重启才刷新）。当前任务实际所用后端另附 running 字段
    如实透出（设置热切换下一任务生效，两者可能不同，前端并排展示）。
    探测首次可达分钟级，放线程池避免阻塞事件循环。"""
    engine_setting = load_settings().get("engine", "auto")
    want = None if engine_setting == "auto" else engine_setting
    loop = asyncio.get_running_loop()
    e = await loop.run_in_executor(None, select_engine, want)
    out = {"backend": e.backend, "python": e.python_exe, "detail": e.detail}
    if runner.current_id is not None and runner.engine is not None:
        r = runner.engine
        out["running"] = {"backend": r.backend, "detail": r.detail}
    return out


@router.get("/api/settings")
def get_settings() -> dict:
    return load_settings()


@router.get("/api/log-tail")
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


@router.put("/api/settings")
def put_settings(body: dict) -> dict:
    try:
        data = save_settings(body)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # 完成动作设置变更：布防中的倒计时与新设置不符时作废（改为 none 也撤）
    if "queue_done_action" in body:
        runner.refresh_queue_action(str(data.get("queue_done_action", "none")))
    return data


@router.post("/api/queue-done/cancel")
def cancel_queue_done() -> dict:
    """手动取消"队列完成后关机/休眠"倒计时（无倒计时=幂等成功）。"""
    return {"ok": runner.cancel_queue_action()}
