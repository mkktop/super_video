"""串行队列调度器：一次只跑一个任务（严格不并发），取消杀进程树。"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from ..paths import TEMP_DIR
from ..utils.process import WINDOWS_CREATE_FLAGS, kill_tree
from . import db
from .engine_select import EngineChoice, select_engine
from .events import EventBus

BACKEND_DIR = Path(__file__).resolve().parents[2]

QUEUE_ACTIONS = ("none", "notify", "shutdown", "sleep")
QUEUE_ACTION_GRACE_S = 60  # 关机/休眠前的反悔窗口（新任务入队/手动取消都可撤）


def _execute_power_action(action: str) -> bool:
    """执行关机/休眠（executor 线程）。测试 monkeypatch 本函数拦截。

    关机用 OS 计时器（/t 5）而非 sleep 定时：本函数返回后 sidecar 可能
    先被 Electron 关闭，OS 计时器不受进程退出影响。休眠没有系统级取消
    语义，倒计时由本进程 asyncio 把守（宽限期内可撤）。
    """
    try:
        if action == "shutdown":
            subprocess.run(
                ["shutdown", "/s", "/t", "5", "/c", "super_video：任务队列已全部完成"],
                check=True, creationflags=WINDOWS_CREATE_FLAGS)
        elif action == "sleep":
            subprocess.run(
                ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                check=True, creationflags=WINDOWS_CREATE_FLAGS)
        else:
            return False
        return True
    except OSError:
        return False


def fps_avg(frames: float, elapsed_s: float) -> float:
    """完成态平均速度（tasks.fps_avg 字段口径）：总帧数÷本轮用时。

    端到端口径（含引擎加载与最终合成）；断点续跑的任务只计最后一轮，
    跳过的段不产生用时，数值会偏高。无有效数据返回 0。
    """
    if not frames or elapsed_s <= 0:
        return 0.0
    return round(frames / elapsed_s, 2)


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
        self._engine_pick = None  # start() 的后台探测 future（首个任务消费，防赋值赛跑）
        self.engine: EngineChoice | None = None  # worker 解释器/后端（start 时决定）
        # ---- 队列完成后动作 ----
        self.loop: asyncio.AbstractEventLoop | None = None  # start() 记下，供线程侧回跳
        self._queue_gen = 0  # 队列活动计数：递增使进行中的倒计时作废
        self._queue_countdown: asyncio.Task | None = None
        self._queue_armed_action: str | None = None

    # ---- 生命周期 ----

    def start(self) -> None:
        # 同进程内 app 可能多次启停（测试、热重启）：必须重建停止信号，
        # 否则上一次 stop() 置位的 _stopping 会让本实例的循环立即退出、队列假死
        self._stopping = asyncio.Event()
        self._cancel_requested = None
        self._reap_orphan_workers()
        recovered = db.recover_running()
        if recovered:
            self.bus.publish({"type": "recovered", "count": recovered})
        # 后端选择（探测 CUDA，耗时最多 2 分钟）放线程里做，不阻塞事件循环；
        # 持有 future 供首个任务 await（避免与 _run_one 的赋值赛跑，/api/engine 闪旧值）
        loop = asyncio.get_running_loop()
        self.loop = loop
        self._engine_pick = loop.run_in_executor(None, select_engine)
        self._loop_task = loop.create_task(self._loop())

    def _reap_orphan_workers(self) -> None:
        """上个 sidecar 被强杀后残留的 worker（含双路分片子进程）：发现即连坐杀。

        孤儿的 stdout 管道已死（事件再也写不出去），放任继续跑只会与重启后的
        新 worker 双写同一任务的分段文件（seg_*.mp4 交错损坏）；杀掉后
        checkpoint 仍在，recover 入队续跑即可。owner.pid 由 _run_one 落盘。
        """
        seg_root = TEMP_DIR / "segmented"
        if not seg_root.is_dir():
            return
        try:
            import psutil
        except ImportError:
            return
        for pid_file in seg_root.glob("*/owner.pid"):
            try:
                pid = int(pid_file.read_text().strip())
                p = psutil.Process(pid)
                if p.is_running() and "worker" in " ".join(p.cmdline()):
                    kill_tree(pid)  # 树杀：带走分片子进程
                    print(f"[runner] 清杀孤儿 worker pid={pid} task={pid_file.parent.name}",
                          flush=True)
            except (OSError, ValueError, psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            finally:
                try:
                    pid_file.unlink(missing_ok=True)
                except OSError:
                    pass

    async def stop(self) -> None:
        self._stopping.set()
        # 完成动作倒计时静默撤除（不发取消事件——进程都在退出了，UI 不必回应）
        if self._queue_countdown is not None and not self._queue_countdown.done():
            self._queue_countdown.cancel()
        self._queue_countdown = None
        self._queue_armed_action = None
        # 正在跑的任务按"已取消"收尾（checkpoint 保留、可续跑），而不是落成
        # failed + "worker 异常退出"的吓人报错——与用户手动取消同语义
        if self.current_id is not None:
            self._cancel_requested = self.current_id
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
                (TEMP_DIR / "segmented" / task["id"] / "owner.pid").unlink(missing_ok=True)
                self.current_id = None
                self.proc = None
                self._arm_queue_action()

    # ---- 队列完成后动作 ----

    def _arm_queue_action(self) -> None:
        """刚有任务落终态：队列已空 → 按设置布防完成动作（仅事件循环线程调用）。

        布防时机是"最后一个任务收尾后"，而不是周期轮询——设置读取次数与
        任务数同阶，空闲队列零开销。失败任务同样触发：用户挂机意图是
        "处理完就关机"，失败也是一种处理完（醒来能看到失败原因）。
        """
        from .settings import load as load_settings

        try:
            action = load_settings().get("queue_done_action", "none")
        except Exception:  # noqa: BLE001 — 设置读不了按无动作，不能挡调度循环
            action = "none"
        if action not in QUEUE_ACTIONS or action == "none":
            return
        if db.has_active():
            return  # 还有排队/运行任务（含双路收尾窗口），谈不上"队列完成"
        self.notify_queue_activity()  # 清掉可能残留的上一轮倒计时
        gen = self._queue_gen
        self._queue_armed_action = action
        self._queue_countdown = asyncio.get_running_loop().create_task(
            self._queue_action_countdown(action, gen))

    async def _queue_action_countdown(self, action: str, gen: int) -> None:
        grace = 0 if action == "notify" else QUEUE_ACTION_GRACE_S
        self.bus.publish({"type": "queue_done", "action": action, "grace_s": grace})
        try:
            await asyncio.sleep(grace)
        except asyncio.CancelledError:
            return  # notify_queue_activity 已发取消事件
        # 双保险：世代号（线程侧活动通知）+ 再查一次队列（布防后塞进来的任务）
        if gen != self._queue_gen or db.has_active():
            return
        ok = True
        if action != "notify":
            loop = asyncio.get_running_loop()
            ok = await loop.run_in_executor(None, _execute_power_action, action)
        self._queue_armed_action = None
        self.bus.publish({"type": "queue_done_fired", "action": action, "ok": ok})

    def notify_queue_activity(self) -> None:
        """队列又有活动（新任务/续跑/设置变更）：作废进行中的完成动作倒计时。

        仅事件循环线程调用；线程池侧（创建端点）走 notify_queue_activity_threadsafe。
        """
        self._queue_gen += 1
        self._queue_armed_action = None
        t, self._queue_countdown = self._queue_countdown, None
        if t is not None and not t.done():
            t.cancel()
            self.bus.publish({"type": "queue_done_canceled"})

    def notify_queue_activity_threadsafe(self) -> None:
        """线程池端点（POST /api/tasks 是同步 def，跑在线程里）的回跳入口。"""
        loop = self.loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self.notify_queue_activity)
            except RuntimeError:
                pass  # loop 在发布前关闭（退出竞态）
        elif loop is None:
            self.notify_queue_activity()  # 未 start（测试直连）：无倒计时可撤，直接走

    def refresh_queue_action(self, new_action: str) -> None:
        """设置变更：布防中的动作与新设置不符时作废倒计时（含改为 none）。"""
        if self._queue_countdown is not None and new_action != self._queue_armed_action:
            self.notify_queue_activity_threadsafe()

    def cancel_queue_action(self) -> bool:
        """手动取消完成动作倒计时；无倒计时返回 False（幂等端点用）。"""
        if self._queue_countdown is None:
            return False
        self.notify_queue_activity_threadsafe()
        return True

    async def _run_one(self, task: dict) -> None:
        task_id = task["id"]
        self.current_id = task_id
        self.bus.publish({"type": "task_status", "task_id": task_id, "status": "running"})

        # 消费 start() 的后台探测（同时把结果给 /api/engine 一个确定性初值）；
        # 之后每个任务仍按当前设置重新选择（设置热切换，下一任务生效）
        pick, self._engine_pick = self._engine_pick, None
        if pick is not None:
            try:
                self.engine = await asyncio.wait_for(asyncio.shield(pick), timeout=300)
            except Exception:  # noqa: BLE001 — 探测失败走下面的每任务选择
                pass
        from .settings import load as load_settings

        engine_setting = load_settings().get("engine", "auto")

        def _select():
            # torch 引擎必须走 PyTorch CUDA 环境（独立 .venv-cuda）
            try:
                from ..models.registry import get_model

                if get_model(task["model_id"]).engine == "torch":
                    return select_engine("cuda")
            except Exception:  # noqa: BLE001 — 模型解析失败交给 worker 报具体错误
                pass
            return select_engine(None if engine_setting == "auto" else engine_setting)

        loop = asyncio.get_running_loop()
        self.engine = await loop.run_in_executor(None, _select)

        # 引擎探测期（可达分钟级）收到取消：不再拉 worker，直接按取消收尾
        if self._cancel_requested == task_id:
            self._cancel_requested = None
            self._cleanup_partial(task)
            db.update_task(task_id, status="canceled")
            self.bus.publish({"type": "task_status", "task_id": task_id, "status": "canceled"})
            return
        worker_py = self.engine.python_exe
        env = {**os.environ, "PYTHONPATH": str(BACKEND_DIR), "PYTHONUNBUFFERED": "1"}
        # frozen worker 的管道 stdout 默认走系统 locale（GBK）：中文事件行会以 GBK
        # 字节到达本进程，UTF-8 解码出 U+FFFD 再回写直接 UnicodeEncodeError——
        # 强制 worker 全链 UTF-8（emit 的 JSON 行才是可信的 UTF-8）
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
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
        # owner.pid：sidecar 被强杀后，下个实例靠它清杀孤儿 worker（_reap_orphan_workers）
        try:
            pid_file = TEMP_DIR / "segmented" / task_id / "owner.pid"
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(str(self.proc.pid))
        except OSError:
            pass

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
            # 真实片尾修正：probe 估算偏大被解码 EOF 证伪时，worker 的 done 事件
            # 带 total_frames 实测值回填（旧 worker/图片路径无此键，按估算值兜底）
            done_total = final.get("total_frames") or t.get("total_frames", 0)
            db.update_task(
                task_id, status="done", out_bytes=final.get("out_bytes", 0),
                preview_path=final.get("preview"),
                preview_src=final.get("src_preview"),
                elapsed_s=final.get("elapsed", 0) or 0,
                fps_avg=fps_avg(final.get("frames", 0), final.get("elapsed", 0) or 0),
                total_frames=done_total,
                progress_frames=done_total,
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
        """取消/失败时删除半成品输出文件，不留给用户。

        图片批量任务例外：逐图原子落盘（.part+replace），取消时已完成的
        文件是完整成果，全部保留——只清可能残留的 .part 临时文件。
        """
        out = Path(task["output_path"])
        if (task.get("params") or {}).get("kind") == "image":
            import glob

            for part in glob.glob(str(out.parent / "*.part")):
                try:
                    Path(part).unlink()
                except OSError:
                    pass
            return
        try:
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
        if t["status"] == "running" and self.current_id == task_id:
            # 探测期（proc 未起）只落标记：_run_one 拉起 worker 前会检查并直接收尾
            self._cancel_requested = task_id
            if self.proc is not None and self.proc.returncode is None:
                kill_tree(self.proc.pid)  # Windows: 硬杀；退出处理按标记归为 canceled
            return True
        return False  # done/failed/canceled 无需取消
