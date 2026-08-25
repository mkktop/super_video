"""串行队列调度器：一次只跑一个任务（严格不并发），取消杀进程树。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from ..utils.process import WINDOWS_CREATE_FLAGS, kill_tree
from . import db
from .engine_select import EngineChoice, select_engine
from .events import EventBus

BACKEND_DIR = Path(__file__).resolve().parents[2]


def error_hint(line: str) -> str | None:
    """worker 非标准输出行 → 任务 error 提示。

    引擎信息行（[engine] 前缀，如 u8 包装生效/后端回退）是正常状态不是错误，
    不能进 error 字段——任务卡会把 error 渲染成"错误信息"吓到用户。
    """
    if not line or line.startswith("[engine]"):
        return None
    return line[-300:]


class Runner:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self.proc: asyncio.subprocess.Process | None = None
        self.current_id: str | None = None
        self._cancel_requested: str | None = None  # Windows 硬杀无法优雅上报，靠标记区分取消/崩溃
        self._stopping = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        self.engine: EngineChoice | None = None  # worker 解释器/后端（start 时决定）

    # ---- 生命周期 ----

    def start(self) -> None:
        # 同进程内 app 可能多次启停（测试、热重启）：必须重建停止信号，
        # 否则上一次 stop() 置位的 _stopping 会让本实例的循环立即退出、队列假死
        self._stopping = asyncio.Event()
        self._cancel_requested = None
        recovered = db.recover_running()
        if recovered:
            self.bus.publish({"type": "recovered", "count": recovered})
        # 后端选择（探测 CUDA，耗时最多 2 分钟）放线程里做，不阻塞事件循环
        loop = asyncio.get_running_loop()

        def _pick():
            self.engine = select_engine()

        loop.run_in_executor(None, _pick)
        self._loop_task = loop.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping.set()
        if self.proc is not None and self.proc.returncode is None:
            kill_tree(self.proc.pid)
        if self._loop_task:
            await asyncio.gather(self._loop_task, return_exceptions=True)

    # ---- 主循环：严格串行 ----

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            task = db.next_queued()
            if task is None:
                await asyncio.sleep(0.5)
                continue
            try:
                await self._run_one(task)
            except Exception as e:  # noqa: BLE001 — 单任务异常不能杀死调度器
                db.update_task(task["id"], status="failed", error=f"runner: {e}")
                self.bus.publish({"type": "task_status", "task_id": task["id"],
                                  "status": "failed", "error": str(e)})
            finally:
                self.current_id = None
                self.proc = None

    async def _run_one(self, task: dict) -> None:
        task_id = task["id"]
        self.current_id = task_id
        self.bus.publish({"type": "task_status", "task_id": task_id, "status": "running"})

        # 每个任务启动时按当前设置选择后端（设置热切换，下一任务生效）
        from .settings import load as load_settings

        engine_setting = load_settings().get("engine", "auto")
        # torch 引擎必须走 PyTorch CUDA 环境（独立 .venv-cuda）
        try:
            from ..models.registry import get_model

            if get_model(task["model_id"]).engine == "torch":
                self.engine = select_engine("cuda")
            else:
                self.engine = select_engine(
                    None if engine_setting == "auto" else engine_setting
                )
        except Exception:  # noqa: BLE001 — 模型解析失败交给 worker 报具体错误
            self.engine = select_engine(None if engine_setting == "auto" else engine_setting)
        worker_py = self.engine.python_exe
        env = {**os.environ, "PYTHONPATH": str(BACKEND_DIR), "PYTHONUNBUFFERED": "1"}
        # 打包版复用 sidecar.exe 自身作为 worker（cli.py worker 子命令）
        if getattr(sys, "frozen", False):
            spawn_cmd = [worker_py, "worker", task_id]
            env.pop("PYTHONPATH", None)
        else:
            spawn_cmd = [worker_py, "-m", "sv.server.worker", task_id]
        self.proc = await asyncio.create_subprocess_exec(
            *spawn_cmd,
            cwd=str(BACKEND_DIR) if not getattr(sys, "frozen", False) else None,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # stderr 混入 stdout 按日志行处理
            creationflags=WINDOWS_CREATE_FLAGS,
        )

        final: dict = {}
        assert self.proc.stdout is not None
        async for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                hint = error_hint(line)
                if hint is not None:
                    db.update_task(task_id, error=hint)
                continue  # 非标准输出行（库的 print 等）忽略
            et = ev.get("type")
            if et == "started":
                db.update_task(task_id, total_frames=ev.get("total_frames", 0))
                self.bus.publish(ev | {"task_id": task_id})
            elif et == "progress":
                db.update_task(
                    task_id, progress_frames=ev.get("frames", 0),
                    fps_run=ev.get("fps", 0), eta_sec=ev.get("eta_sec", 0),
                )
                self.bus.publish(ev | {"task_id": task_id})
            elif et in ("done", "failed", "canceled"):
                final = ev
            elif et == "log":
                # 状态类日志（TRT 编译提示等）落 sidecar 日志（日志页可见），不进任务 error
                print(f"[task:{task_id[:8]}] {ev.get('line', '')}", flush=True)
                self.bus.publish(ev | {"task_id": task_id})
            else:
                self.bus.publish(ev | {"task_id": task_id})

        rc = await self.proc.wait()
        canceled_by_user = self._cancel_requested == task_id
        self._cancel_requested = None

        # 以 worker 显式上报的事件为准；Windows 硬杀时靠取消标记兜底
        if final.get("type") == "done":
            t = db.get_task(task_id) or {}
            db.update_task(
                task_id, status="done", out_bytes=final.get("out_bytes", 0),
                preview_path=final.get("preview"),
                preview_src=final.get("src_preview"),
                elapsed_s=final.get("elapsed", 0) or 0,
                progress_frames=t.get("total_frames", 0),
                error=None,  # 成功任务不能残留运行期的日志尾部
            )
            self.bus.publish({"type": "task_status", "task_id": task_id, "status": "done"})
        elif final.get("type") == "canceled" or rc == 3 or canceled_by_user:
            self._cleanup_partial(task)
            db.update_task(task_id, status="canceled")
            self.bus.publish({"type": "task_status", "task_id": task_id, "status": "canceled"})
        else:
            self._cleanup_partial(task)
            err = final.get("error") or f"worker 异常退出 (rc={rc})"
            db.update_task(task_id, status="failed", error=err)
            self.bus.publish({"type": "task_status", "task_id": task_id,
                              "status": "failed", "error": err})

    def _cleanup_partial(self, task: dict) -> None:
        """取消/失败时删除半成品输出文件，不留给用户。"""
        try:
            out = Path(task["output_path"])
            if out.exists():
                out.unlink()
        except OSError:
            pass

    # ---- 取消 ----

    async def cancel(self, task_id: str) -> bool:
        t = db.get_task(task_id)
        if t is None:
            return False
        if t["status"] == "queued":
            db.update_task(task_id, status="canceled")
            self.bus.publish({"type": "task_status", "task_id": task_id, "status": "canceled"})
            return True
        if t["status"] == "running" and self.current_id == task_id and self.proc is not None:
            self._cancel_requested = task_id
            kill_tree(self.proc.pid)  # Windows: 硬杀；退出处理按标记归为 canceled
            return True
        return False  # done/failed/canceled 无需取消
