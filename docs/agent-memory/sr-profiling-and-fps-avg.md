---
name: sr-profiling-and-fps-avg
description: v0.2.8 后新增 sr_profiling 性能日志与 tasks.fps_avg
  字段；两者口径都是端到端（含引擎加载+合成），续跑任务 fps_avg 会偏高
metadata:
  node_type: memory
  type: project
  originSessionId: sess_993034a7-a29b-4202-9019-ffb00204c92f
---

2026-08-27 落地两个功能（用户需求：任务卡显示完成态平均速度 + 设置可开瓶颈分析日志）：

- **tasks.fps_avg 列**（db 轻量迁移）：runner 收到 worker done 事件时按 `frames/elapsed` 落定，`sv/server/runner.py` 的 `fps_avg()` 是口径定义。**端到端口径：含引擎加载与最终合成**；断点续跑只计最后一轮（跳过的段不产生用时），数值偏高——UI 悬停提示里已写明，用户拿数字对账时先确认是否续跑过（见 [[perf-numbers-verify-with-elapsed]]）。
- **sr_profiling 设置**：开启后 worker 把"引擎配置头 + SegmentedPipeline 的 perf_stages.jsonl（分段耗时明细，本来就有但成功后随工作目录删除）+ 端到端汇总"落到 `SR_LOG_DIR = DATA_ROOT/data/sr_logs/<task_id>.log`（不在 .tmp 下，逃过孤儿清扫）。图片任务另有 解码/推理/落盘 三段合计。剖析模式 pipeline cleanup=False，worker 收集后自行删工作目录；失败/取消路径照旧保留目录供续跑。
- 接口：`GET /api/tasks/{id}/sr-log`（404=无）；`/api/tasks` 与 `/api/tasks/{id}` 响应带 `has_sr_log`（一次 listdir 标注）。启动 GC 只留最近 200 份（`gc_sr_logs`），删除任务时一并清日志。
- torch(chunked) 路径只有头+尾行（无分段明细文件）；双路并行时只有协调进程写持久日志，分片子进程只写工作目录 jsonl，由协调者合并收集。
