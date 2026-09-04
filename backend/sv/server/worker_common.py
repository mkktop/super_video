"""Worker 共享协议层：stdout JSON 事件输出 + 超分性能日志（原 worker.py 内联段）。

事件协议（每行一个 JSON）：
  {"type":"started","total_frames":n,"output":...}
  {"type":"progress","frames":n,"total":n,"fps":f,"eta_sec":e}
  {"type":"log","line":"..."}
  {"type":"done","frames":n,"elapsed":s,"out_bytes":b}
  {"type":"failed","error":"..."}
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from sv.paths import SR_LOG_DIR
from sv.server import settings


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def emit_failed(prefix: str, e: Exception) -> None:
    """失败事件 + 完整堆栈落 sidecar.log（任务 error 保持单行可读）。

    runner 把 log 事件打到 sidecar.log（日志页可见）——异常被本层捕获后
    只有一行 error 上达任务卡，没有堆栈时「引擎加载失败 UnicodeDecodeError」
    这类问题只能靠猜（1060 实机教训）；堆栈行号能直接定位裸奔的读取点。
    注意 emit 的是 JSON 行：堆栈里不能有裸 \r，json.dumps 会转义，安全。
    """
    import traceback

    tb = traceback.format_exc()
    emit({"type": "log", "line": f"{prefix} {type(e).__name__} 堆栈:\n{tb.strip()}"})
    emit({"type": "failed", "error": f"{prefix} {type(e).__name__}: {e}"})


# ---- 超分性能日志（设置 sr_profiling 开启时生效）----
# 任务结束把"引擎配置 + 分段耗时明细 + 汇总"落到 SR_LOG_DIR/<task_id>.log，
# 供分析速度瓶颈（推理慢/解码跟不上/编码拖后腿/引擎加载占比）。旁路功能：
# 任何写入失败静默忽略，绝不影响任务本身。


def _prof_enabled() -> bool:
    return bool(settings.load().get("sr_profiling"))


def _prof_write(task_id: str, text: str) -> None:
    try:
        SR_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (SR_LOG_DIR / f"{task_id}.log").open("a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def _prof_write_video_header(task_id: str, ctx: dict) -> None:
    seg_txt = (f"分段 ≈{ctx['seg']}帧 x {ctx['n_segs']}段" if ctx["n_segs"]
               else f"分块 {ctx['seg']}帧")
    lines = [
        f"==== {time.strftime('%Y-%m-%d %H:%M:%S')} 运行开始 ====",
        f"模型 {ctx['model']} · 推理后端 {ctx['provider']} · 精度 {ctx['precision']}"
        f" · GPU前后处理包装 {'开' if ctx['u8'] else '关'}",
        f"设置 engine={ctx['engine_setting']} · tile={ctx['tile']} · batch={ctx['batch']}"
        f" · 补帧={ctx['interp']} · 解码={ctx.get('decoder', 'sw')}"
        f" · 预处理={ctx.get('prefilter') or '无'}",
        f"源 {ctx['src_w']}x{ctx['src_h']} @{ctx['fps']:.3f}fps {ctx['frames']}帧"
        f" → 目标 {ctx['target']}",
        f"引擎加载+预热 {ctx['load_s']:.1f}s · {seg_txt}"
        f" · 双路并行 {'是(2进程)' if ctx['parallel'] else '否'}"
        f" · 断点续跑 {'是(跳过已完成段)' if ctx['resumed'] else '否'}",
    ]
    _prof_write(task_id, "\n".join(lines) + "\n")


def _prof_collect(task_id: str, work: Path) -> None:
    """任务成功收尾：把工作目录里的分段耗时明细（SegmentedPipeline 逐段落盘）
    并入持久日志。工作目录本身的清理仍由调用方原逻辑负责。"""
    perf_file = work / "perf_stages.jsonl"
    try:
        if perf_file.exists():
            body = perf_file.read_text(encoding="utf-8")
            _prof_write(task_id,
                        "---- 分段耗时明细（jsonl，每行一段；summary 行 ms_per_frame"
                        " 为各阶段毫秒/帧拆解，infer=推理 read=解码 write=编码）----\n"
                        + body)
    except OSError:
        pass

