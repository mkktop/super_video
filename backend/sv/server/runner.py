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
from .gpu_lease import release as _gpu_release
from .gpu_lease import try_acquire as _gpu_try
from .events import EventBus

BACKEND_DIR = Path(__file__).resolve().parents[2]

QUEUE_ACTIONS = ("none", "notify", "shutdown", "sleep")
QUEUE_ACTION_GRACE_S = 60  # 关机/休眠前的反悔窗口（新任务入队/手动取消都可撤）

SCHEDULE_MODES = ("always", "window", "idle")


def _windows_idle_seconds() -> float:
    """距上次键鼠输入的秒数（GetLastInputInfo，系统 API 零依赖）。

    探测失败/取值异常按 0（不空闲）处理：闲时模式宁可多跑不误挂起——
    挂住了用户会以为软件坏了。GetTickCount 49.7 天回绕产生的巨大差值
    同样按不空闲丢弃。
    """
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        lii = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO))
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            millis = int(ctypes.windll.kernel32.GetTickCount()) - int(lii.dwTime)
            if 0 <= millis < 2**31:
                return millis / 1000.0
    except Exception:  # noqa: BLE001 — 非 Windows/权限异常一律按不空闲
        pass
    return 0.0


def _parse_hhmm(s: str) -> tuple[int, int] | None:
    try:
        h, m = str(s).split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError):
        pass
    return None


def _in_window(now: tuple[int, int], start: tuple[int, int], end: tuple[int, int]) -> bool:
    """时段判断（分钟数比较）。start==end 视为全天放行（配置退化的宽容解）；
    start<=end 常规区间；start>end 跨午夜（如 22:00~08:00）。"""
    t = now[0] * 60 + now[1]
    a = start[0] * 60 + start[1]
    b = end[0] * 60 + end[1]
    if a == b:
        return True
    if a < b:
        return a <= t < b
    return t >= a or t < b


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


def _worker_spawn_cmd(worker_py: str, task_id: str) -> list[str]:
    """worker 拉起命令。--serve=常驻模式：done 后经 stdin 接续下一任务，
    进程内引擎缓存跨任务生效；frozen 复用 sidecar.exe 自身（cli.py worker）。"""
    if getattr(sys, "frozen", False):
        return [worker_py, "worker", task_id, "--serve"]
    return [worker_py, "-m", "sv.server.worker", task_id, "--serve"]


def _delete_source_if_enabled(task: dict) -> None:
    """设置 delete_source_after_done 开启时，成功收尾后删除源文件。

    视频任务删 input_path；图片批量任务按 params.images 清单逐张删。
    删除是收尾附加动作，失败不回滚任务（成果已落盘），只落 sidecar 日志
    留痕；输出与源同路径的假想碰撞（创建期命名纪律本就会避开）不删防误伤。
    """
    from .settings import load as load_settings

    if not load_settings().get("delete_source_after_done"):
        return
    params = task.get("params") or {}
    if params.get("kind") == "image":
        items = [(i.get("in"), i.get("out"))
                 for i in params.get("images") or [] if isinstance(i, dict)]
    else:
        items = [(task.get("input_path"), task.get("output_path"))]
    for src, dst in items:
        if not src:
            continue
        try:
            # 输出与源同路径的假想碰撞（创建期命名纪律本就会避开）不删防误伤
            if dst and os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
                continue
            os.remove(src)
            print(f"[task:{task['id'][:8]}] 已删除源文件 {src}", flush=True)
        except (OSError, ValueError) as e:
            print(f"[task:{task['id'][:8]}] 删除源文件失败 {src}: {e}", flush=True)


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
        self._gate_state: tuple[bool, str] | None = None  # 处理时机闸门（None=未判定）

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

    # ---- 主循环：严格串行（处理时机闸门只拦"开始下一个任务"） ----

    def gate_state(self) -> dict:
        """当前放行状态（stats 端点透出；未判定按放行）。"""
        active, reason = self._gate_state or (True, "")
        return {"active": active, "reason": reason}

    def _queue_gate(self) -> tuple[bool, str]:
        """是否允许领取新任务：always 立即 / window 指定时段 / idle 键鼠静置。

        只拦"开始下一个任务"，不打断进行中的任务——checkpoint 语义安全，
        代价是闸门关上前已开跑的任务会跑完（通常正是期望：别留半成品）。
        """
        from datetime import datetime

        from .settings import load as load_settings

        try:
            s = load_settings()
            mode = s.get("queue_schedule", "always")
            if mode == "window":
                start = _parse_hhmm(str(s.get("schedule_start", "22:00")))
                end = _parse_hhmm(str(s.get("schedule_end", "08:00")))
                if start is None or end is None:
                    return True, ""  # 配置坏了不拦队列（宽容解）
                now = datetime.now()
                ok = _in_window((now.hour, now.minute), start, end)
                label = f"{start[0]:02d}:{start[1]:02d}~{end[0]:02d}:{end[1]:02d}"
                return ok, ("" if ok else f"等待处理时段（{label}）")
            if mode == "idle":
                need = max(1, int(s.get("idle_minutes", 15) or 15))
                ok = _windows_idle_seconds() >= need * 60
                return ok, ("" if ok else f"等待电脑空闲（键鼠静置 {need} 分钟后开始）")
        except Exception:  # noqa: BLE001 — 设置读不了按放行，闸门绝不卡死队列
            pass
        return True, ""

    def _report_gate(self, active: bool, reason: str) -> None:
        """闸门状态变化才广播（关着时 5s 一查，不变不刷事件）。"""
        state = (active, reason)
        if state != self._gate_state:
            self._gate_state = state
            self.bus.publish({"type": "queue_gate", "active": active, "reason": reason})

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            gate_open, reason = self._queue_gate()
            if not gate_open:
                self._report_gate(False, reason)
                await asyncio.sleep(5.0)
                continue
            self._report_gate(True, "")
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
        """GPU 租约门卫：拿到租约才进真实执行（_run_one_guarded），全程持有。

        等待期间（compare/trim 作业在跑）任务已置 running、可正常取消；
        finally 释放保证任何异常路径都不把租约带给下一个任务。
        """
        task_id = task["id"]
        self.current_id = task_id
        while not _gpu_try("task"):
            if self._cancel_requested == task_id:
                self._cancel_requested = None
                self._cleanup_partial(task)
                db.update_task(task_id, status="canceled")
                self.bus.publish({"type": "task_status", "task_id": task_id, "status": "canceled"})
                return
            await asyncio.sleep(0.5)
        try:
            await self._run_one_guarded(task)
        finally:
            _gpu_release("task")

    async def _run_one_guarded(self, task: dict) -> None:
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
        spawn_cmd = _worker_spawn_cmd(worker_py, task_id)
        if getattr(sys, "frozen", False):
            env.pop("PYTHONPATH", None)
        self.proc = await asyncio.create_subprocess_exec(
            *spawn_cmd,
            cwd=str(BACKEND_DIR) if not getattr(sys, "frozen", False) else None,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,  # 常驻模式：done 后经此喂下一任务 id
            stderr=asyncio.subprocess.STDOUT,  # stderr 混入 stdout 按日志行处理
            creationflags=WINDOWS_CREATE_FLAGS,
        )
        # owner.pid：sidecar 被强杀后，下个实例靠它清杀孤儿 worker（_reap_orphan_workers）
        self._write_owner_pid(task_id)

        # 常驻 worker 事件泵：done 且队列还有任务且闸门放行 → 经 stdin 续喂下一个
        # 任务（进程不退出，进程内引擎缓存跳过同签名任务的加载+预热）；其余终态
        # 关 stdin 丢弃进程——失败/取消后会话健康度不可信（OOM 即损坏级联）。
        cur = task
        final: dict = {}
        writer = self.proc.stdin
        while True:
            final = await self._pump_until_final(cur)
            reuse_ok = (
                final.get("type") == "done"
                and not self._stopping.is_set()
                and self._queue_gate()[0]
            )
            if not reuse_ok:
                break
            nxt = db.next_queued()
            if nxt is None:
                break
            self._finalize_done(cur, final)
            cur = nxt
            self.current_id = nxt["id"]
            self._write_owner_pid(nxt["id"])  # 每个续喂任务各留自己的孤儿清杀依据
            self.bus.publish({"type": "task_status", "task_id": nxt["id"], "status": "running"})
            try:
                writer.write((nxt["id"] + "\n").encode())
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, RuntimeError):
                break  # worker 意外退出：下一轮 wait() 按退出码收尾
        try:
            if writer is not None:
                writer.close()  # 结束常驻：worker 读到 EOF 优雅退出
        except (ConnectionResetError, BrokenPipeError, RuntimeError):
            pass

        rc = await self.proc.wait()
        canceled_by_user = self._cancel_requested == cur["id"]
        self._cancel_requested = None

        # 以 worker 显式上报的事件为准；Windows 硬杀时靠取消标记兜底
        if final.get("type") == "done":
            self._finalize_done(cur, final)
        elif final.get("type") == "canceled" or rc == 3 or canceled_by_user:
            self._cleanup_partial(cur)
            db.update_task(cur["id"], status="canceled")
            self.bus.publish({"type": "task_status", "task_id": cur["id"], "status": "canceled"})
        else:
            self._cleanup_partial(cur)
            err = final.get("error") or f"worker 异常退出 (rc={rc})"
            db.update_task(cur["id"], status="failed", error=err)
            self.bus.publish({"type": "task_status", "task_id": cur["id"],
                              "status": "failed", "error": err})

    async def _pump_until_final(self, task: dict) -> dict:
        """泵当前任务的 worker 事件直到终态事件（started/progress/log 转发并落库）。

        常驻模式下 stdout 是跨任务连续流：终态事件只标记本任务结束、不断流，
        下一任务的事件由下一次调用继续消费。"""
        task_id = task["id"]
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
                return ev
            elif et == "log":
                # 状态类日志（TRT 编译提示等）落 sidecar 日志（日志页可见），不进任务 error
                print(f"[task:{task_id[:8]}] {ev.get('line', '')}", flush=True)
                self.bus.publish(ev | {"task_id": task_id})
            else:
                self.bus.publish(ev | {"task_id": task_id})
        return {}  # 流意外结束（硬杀/崩溃）：无终态事件，调用方按退出码兜底

    def _write_owner_pid(self, task_id: str) -> None:
        try:
            pid_file = TEMP_DIR / "segmented" / task_id / "owner.pid"
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(str(self.proc.pid))
        except OSError:
            pass

    def _finalize_done(self, task: dict, final: dict) -> None:
        task_id = task["id"]
        t = db.get_task(task_id) or {}
        # 真实片尾修正：probe 估算偏大被解码 EOF 证伪时，worker 的 done 事件
        # 带 total_frames 实测值回填（旧 worker/图片路径无此键，按估算值兜底）
        done_total = final.get("total_frames") or t.get("total_frames", 0)
        # 先删源再落 done：状态可见时删除已完成，「done」是自洽终态——
        # UI 首次刷新即看到 input_exists=false，对比入口同帧置灰，
        # 不存在"列表已显示完成、源却还在/点进对比才发现没了"的窗口
        _delete_source_if_enabled(task)
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
